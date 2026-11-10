# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:12:00
# @Author  : yangyuexiong
# @File    : tool_execution.py

"""以数据库写锁和有界租约管理跨 API、worker 的工具并发。"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from uuid import uuid4

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_config
from app.core.enums import ToolLeaseKind
from app.models.base import shanghai_now
from app.models.tool_execution import ToolExecutionGate, ToolExecutionLease


CURRENT_TOOL_CALL_ID: ContextVar[str | None] = ContextVar("tool_call_id", default=None)


class ToolCapacityError(ValueError):
    """并发名额已满；调用立即拒绝，准备任务保留排队状态。"""


async def lock_tool_capacity(db: AsyncSession) -> None:
    """先创建再写入固定闸门行，三种数据库都在事务内持锁。"""
    table = ToolExecutionGate.__table__
    values = {
        "id": 1,
        "revision": 0,
    }
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=[table.c.id])
    elif dialect == "mysql":
        statement = mysql_insert(table).values(**values).on_duplicate_key_update(revision=table.c.revision)
    else:
        statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=[table.c.id])
    await db.execute(statement)
    await db.execute(update(ToolExecutionGate).where(ToolExecutionGate.id == 1).values(revision=ToolExecutionGate.revision + 1))


async def acquire_tool_slot(db: AsyncSession, owner_id: int, run_id: str | None, kind: ToolLeaseKind, timeout_seconds: int) -> str:
    """原子核对各级上限并占用名额，调用方必须提交当前事务。"""
    await lock_tool_capacity(db)
    now = shanghai_now(naive=True)
    await db.execute(delete(ToolExecutionLease).where(ToolExecutionLease.expires_at <= now))
    config = get_config()
    if kind == ToolLeaseKind.CALL:
        limits = [(None, config.TOOL_CALL_GLOBAL_LIMIT), (ToolExecutionLease.owner_id == owner_id, config.TOOL_CALL_USER_LIMIT)]
        if run_id is not None:
            limits.append((ToolExecutionLease.run_id == run_id, config.TOOL_CALL_RUN_LIMIT))
    else:
        limits = [
            (None, config.TOOL_PREPARATION_GLOBAL_LIMIT),
            (ToolExecutionLease.owner_id == owner_id, config.TOOL_PREPARATION_USER_LIMIT),
        ]
    for condition, limit in limits:
        query = select(ToolExecutionLease.id).where(ToolExecutionLease.kind == kind)
        if condition is not None:
            query = query.where(condition)
        if len((await db.scalars(query.limit(limit).with_for_update())).all()) >= limit:
            raise ToolCapacityError("工具资源并发已满，请稍后重试")
    token = str(uuid4())
    db.add(
        ToolExecutionLease(
            token=token,
            kind=kind,
            owner_id=owner_id,
            run_id=run_id,
            expires_at=now + timedelta(seconds=timeout_seconds + 30),
            is_deleted=0,
            status=1,
        ),
    )
    await db.flush()
    return token


async def release_tool_slot(sessions: async_sessionmaker[AsyncSession], token: str) -> None:
    """按令牌释放名额；数据库故障时保留到期自动回收。"""
    async with sessions.begin() as db:
        await db.execute(delete(ToolExecutionLease).where(ToolExecutionLease.token == token))


@asynccontextmanager
async def tool_execution_slot(sessions: async_sessionmaker[AsyncSession], owner_id: int, run_id: str | None, kind: ToolLeaseKind, timeout_seconds: int):
    """在成功、异常及取消路径释放名额，强杀后依靠持久化到期时间回收。"""
    async with sessions.begin() as db:
        token = await acquire_tool_slot(db, owner_id, run_id, kind, timeout_seconds)
    try:
        yield token
    finally:
        task = asyncio.create_task(release_tool_slot(sessions, token))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
        except Exception as exc:
            logger.error("工具资源租约释放失败：{}", type(exc).__name__)
