# -*- coding: utf-8 -*-
# @Time    : 2026-11-24 23:27:00
# @Author  : yangyuexiong
# @File    : ag_ui_history.py

"""公开历史读取、终态投影缓存和消息身份检查；调用者先检查会话授权。"""

from uuid import UUID

from sqlalchemy import String, cast, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import MessageRole
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_run_public_history import AgentRunPublicHistory
from app.services.ag_ui_projection import load_projection, projection_messages
from app.services.run_manager import TERMINAL_STATES

# 修改投影消息的身份、结构或内容时递增，避免复用旧实现的派生结果。
PUBLIC_HISTORY_PROJECTION_VERSION = 1


async def _cache_messages(db: AsyncSession, run: AgentRun, messages: list[dict]) -> None:
    """同事务写入可重建缓存；并发冷读取不因首次插入竞争失败。"""
    values = {
        "event_seq": run.event_seq,
        "projection_version": PUBLIC_HISTORY_PROJECTION_VERSION,
        "messages": messages,
    }
    condition = AgentRunPublicHistory.run_id == run.id
    writable = or_(
        AgentRunPublicHistory.event_seq <= run.event_seq,
        AgentRunPublicHistory.projection_version != PUBLIC_HISTORY_PROJECTION_VERSION,
    )
    result = await db.execute(update(AgentRunPublicHistory).where(condition, writable).values(**values).execution_options(synchronize_session=False))
    if result.rowcount:
        return
    if await db.scalar(select(AgentRunPublicHistory.run_id).where(condition)) is not None:
        return
    try:
        async with db.begin_nested():
            db.add(AgentRunPublicHistory(run_id=run.id, **values))
            await db.flush()
    except IntegrityError:
        await db.execute(update(AgentRunPublicHistory).where(condition, writable).values(**values).execution_options(synchronize_session=False))


async def public_history(
    db: AsyncSession,
    session_id: str,
    owner_id: int,
    *,
    through_message_id: int | None = None,
    event_through: tuple[str, int] | None = None,
) -> list[dict]:
    """批量读缓存，按固定上界投影失效或非终态 Run；不提交调用者事务。"""
    query = (
        select(AgentRunMessage, AgentRun, AgentRunPublicHistory)
        .join(AgentRun, AgentRun.id == AgentRunMessage.run_id)
        .outerjoin(AgentRunPublicHistory, AgentRunPublicHistory.run_id == AgentRun.id)
        .where(AgentRun.session_id == session_id, AgentRun.owner_id == owner_id, AgentRunMessage.role == MessageRole.USER)
        .order_by(AgentRunMessage.id)
    )
    if through_message_id is not None:
        query = query.where(AgentRunMessage.id <= through_message_id)
    history = []
    for user_message, run, cached in (await db.execute(query)).all():
        history.append(
            {
                "id": user_message.ag_ui_message_id or str(user_message.id),
                "role": "user",
                "content": user_message.content,
            }
        )
        through = event_through[1] if event_through and event_through[0] == run.id else run.event_seq
        if (
            run.state in TERMINAL_STATES
            and cached is not None
            and cached.event_seq == through
            and cached.projection_version == PUBLIC_HISTORY_PROJECTION_VERSION
        ):
            history.extend(cached.messages)
            continue
        _, frames = await load_projection(db, run, through=through)
        messages = projection_messages(frames)
        history.extend(messages)
        if run.state in TERMINAL_STATES and through == run.event_seq:
            await _cache_messages(db, run, messages)
    return history


async def public_message_id_exists(db: AsyncSession, session_id: str, owner_id: int, message_id: str) -> bool:
    """普通用户 ID 不重放事件；UUID5 候选兼容检查服务器确定性投影 ID。"""
    query = (
        select(AgentRunMessage.id)
        .join(AgentRun, AgentRun.id == AgentRunMessage.run_id)
        .where(
            AgentRun.session_id == session_id,
            AgentRun.owner_id == owner_id,
            func.coalesce(AgentRunMessage.ag_ui_message_id, cast(AgentRunMessage.id, String)) == message_id,
        )
    )
    if await db.scalar(query.limit(1)) is not None:
        return True
    # RunProjection.identity 只生成规范 UUID5；其他 ID 不可能与派生消息冲突。
    try:
        identifier = UUID(message_id)
    except ValueError:
        return False
    if identifier.version != 5 or str(identifier) != message_id:
        return False
    return any(message["id"] == message_id for message in await public_history(db, session_id, owner_id))
