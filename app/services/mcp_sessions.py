# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : mcp_sessions.py

"""由 Worker 持有 Run 的 MCP 会话；持久化归属、串行调用并显式终止丢失状态的运行。"""

import asyncio
import base64
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import hmac
import json
import time

from sqlalchemy import or_, select, update, delete
from cryptography.fernet import Fernet
from uuid import uuid4

from app.core.config import get_config
from app.core.enums import ApprovalState, RunState, RunRuntimeFailureReason
from app.models.agent_run import AgentRun
from app.models.run_approval import RunApproval
from app.models.run_mcp_resource import RunMcpResource
from app.models.base import shanghai_now
from app.services.run_manager import TERMINAL_STATES, RunConflictError, _change_state
from app.services.tool_delivery import ToolNotSentError


class McpSessionLostError(ToolNotSentError):
    """会话状态不可恢复；本次未发送业务请求，不能悄悄新建会话继续原任务。"""


@dataclass
class _Entry:
    """同一任务内进入与退出 SDK 传输上下文，避免跨任务取消作用域。"""

    run_id: str
    key: tuple
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    created: float = field(default_factory=time.monotonic)
    touched: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RunMcpSessions:
    """小接口封装连接归属、审批保活、资源配额、调用顺序与退出回收。"""

    def __init__(self, sessions, worker_id: str) -> None:
        """每个 Worker 启动身份只构造一个池；重启不能继承进程内协议状态。"""
        self.sessions = sessions
        self.worker_id = worker_id
        self.entries: dict[tuple, _Entry] = {}
        self.lost: set[str] = set()
        self.lock = asyncio.Lock()
        self.closed = False

    async def _own(self, run_id: str, owner_id: int, lease_token: str, *, recheck_tools: bool = False) -> None:
        """业务租约与资源归属分离；审批只释放业务租约，不转移 MCP 状态。"""
        async with self.sessions.begin() as db:
            from app.services.tool_registry import ToolContext

            run = await ToolContext(self.sessions, run_id, owner_id, lease_token).require_active(db, enabled=lambda item: item.id == run_id)
            if recheck_tools:
                from app.services.user_tools import load_run_tools

                await load_run_tools(db, run_id, owner_id)
            if run is None or run.owner_id != owner_id or run.state != RunState.RUNNING or run.lease_token != lease_token:
                raise RunConflictError("MCP 会话调用未持有运行租约")
            now = shanghai_now(naive=True)
            if run.mcp_worker_id is not None and (run.mcp_worker_id != self.worker_id or run.mcp_owner_expires_at is None or run.mcp_owner_expires_at <= now):
                raise McpSessionLostError("MCP 会话拥有者已失效；原业务状态不能自动转移")
            result = await db.execute(
                update(AgentRun)
                .where(
                    AgentRun.id == run_id,
                    AgentRun.state == RunState.RUNNING,
                    AgentRun.lease_token == lease_token,
                    AgentRun.lease_expires_at > now,
                    (AgentRun.mcp_worker_id.is_(None) | (AgentRun.mcp_owner_expires_at > now)),
                    (AgentRun.mcp_worker_id.is_(None)) | (AgentRun.mcp_worker_id == self.worker_id),
                )
                .values(mcp_worker_id=self.worker_id, mcp_owner_expires_at=now + timedelta(seconds=30))
            )
            if result.rowcount != 1:
                raise RunConflictError("MCP 会话归属竞争失败")

    async def invoke(
        self, *, run_id: str, owner_id: int, lease_token: str, connection_id: int | str, config, credentials, factory, operation, config_version=None
    ):
        """按用户、Run、连接和配置凭据指纹隔离；同连接的业务请求串行执行。"""
        if self.closed or run_id in self.lost:
            raise McpSessionLostError("MCP 会话已关闭或过期，请核实已执行结果后发起新任务")
        await self._own(run_id, owner_id, lease_token)
        fingerprint = hmac.new(
            get_config().SECRET_KEY.encode(), json.dumps([config, credentials, config_version], sort_keys=True).encode(), hashlib.sha256
        ).hexdigest()
        key = (owner_id, run_id, connection_id, fingerprint)
        async with self.lock:
            if self.closed or run_id in self.lost:
                raise McpSessionLostError("MCP 会话已被清理，业务调用尚未发送")
            # 同一连接的执行配置或凭据版本变动不能继承原状态。
            if any(item.key[:3] == key[:3] and item.key != key for item in self.entries.values()):
                self.lost.add(run_id)
                raise McpSessionLostError("同一连接的配置或凭据已变化，不能复用原会话")
            entry = self.entries.get(key)
            if entry is None:
                config_limit = get_config()
                if (
                    len(self.entries) >= config_limit.MCP_SESSION_MAX_PER_WORKER
                    or sum(item.run_id == run_id for item in self.entries.values()) >= config_limit.MCP_SESSION_MAX_PER_RUN
                ):
                    raise ToolNotSentError("MCP 会话资源已达上限，请结束其他任务后重试")
                entry = _Entry(run_id, key)
                self.entries[key] = entry
                entry.task = asyncio.create_task(self._serve(entry, factory, config, credentials))
        async with entry.lock:
            await self._own(run_id, owner_id, lease_token, recheck_tools=True)
            now = time.monotonic()
            if (
                run_id in self.lost
                or entry.task.done()
                or now - entry.touched >= get_config().MCP_SESSION_IDLE_SECONDS
                or now - entry.created >= get_config().MCP_SESSION_MAX_SECONDS
            ):
                await self.close_run(run_id, lost=True)
                raise McpSessionLostError("MCP 会话已失效，不能自动重建浏览器等业务状态")
            future = asyncio.get_running_loop().create_future()
            entry.touched = now
            await entry.queue.put((operation, future))
            try:
                return await future
            except BaseException:
                await self.close_run(run_id, lost=True)
                raise
            finally:
                entry.touched = time.monotonic()

    async def _serve(self, entry: _Entry, factory, config, credentials) -> None:
        """连接、SDK 及每次操作都在拥有者任务中，调用失败后终止会话。"""
        pending = None
        resource_id = None
        control = None
        try:
            async with factory() as session:
                control = getattr(session, "_cancel_remote", None)
                session_id = control.get_session_id() if hasattr(control, "get_session_id") else None
                if session_id:
                    now = shanghai_now(naive=True)
                    resource_id = str(uuid4())
                    payload = json.dumps(
                        {
                            "url": config,
                            "credentials": credentials,
                            "session_id": session_id,
                            "protocol_version": session._protocol_version,
                        }
                    )
                    async with self.sessions.begin() as db:
                        db.add(
                            RunMcpResource(
                                id=resource_id,
                                run_id=entry.run_id,
                                encrypted_payload=_cipher().encrypt(payload.encode()).decode(),
                                retired=False,
                                expires_at=now + timedelta(seconds=get_config().MCP_SESSION_MAX_SECONDS),
                                next_attempt_at=now,
                            )
                        )
                while True:
                    operation, pending = await entry.queue.get()
                    result = await operation(session)
                    if not pending.done():
                        pending.set_result(result)
                    pending = None
        except BaseException as exc:
            self.lost.add(entry.run_id)
            if pending is not None and not pending.done():
                pending.set_exception(exc if isinstance(exc, Exception) else McpSessionLostError("MCP 会话被中断"))
            while not entry.queue.empty():
                _, waiting = entry.queue.get_nowait()
                if not waiting.done():
                    waiting.set_exception(McpSessionLostError("MCP 协议初始化失败或连接已丢失"))
            # 初始化期间队列尚未来到也必须让调用者看见 task.done()。
        finally:
            if resource_id is not None:
                # 已确认删除才丢弃凭单；数据库不可用时保留给其他 Worker，不覆盖业务结果。
                with suppress(Exception):
                    async with self.sessions.begin() as db:
                        if getattr(control, "terminated", False):
                            await db.execute(delete(RunMcpResource).where(RunMcpResource.id == resource_id))
                        else:
                            await db.execute(update(RunMcpResource).where(RunMcpResource.id == resource_id).values(retired=True))

    def approval_ttl(self, run_id: str) -> int:
        """审批展示的截止时间不晚于本轮任何既有会话的空闲或累计截止时间。"""
        now = time.monotonic()
        remaining = [
            min(entry.touched + get_config().MCP_SESSION_IDLE_SECONDS - now, entry.created + get_config().MCP_SESSION_MAX_SECONDS - now)
            for entry in self.entries.values()
            if entry.run_id == run_id
        ]
        return max(1, min(3600, int(min(remaining)))) if remaining else 3600

    async def close_run(self, run_id: str, *, lost: bool = False) -> None:
        """取消会触发协议尽力通知及传输退出；不能撤回远端副作用。"""
        if lost:
            self.lost.add(run_id)
        async with self.lock:
            entries = [entry for entry in self.entries.values() if entry.run_id == run_id]
            for entry in entries:
                self.entries.pop(entry.key, None)
        for entry in entries:
            entry.task.cancel()
        if entries:
            closing = asyncio.gather(*(entry.task for entry in entries), return_exceptions=True)
            # 外层工具预算和 MCP 预算可能同时取消调用者，不能把第二次取消传给资源拥有者。
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    continue

    async def sweep(self) -> None:
        """审批期间保活归属，撤权、终态、空闲和累计时限统一收口。"""
        for run_id in list(self.lost):
            async with self.sessions.begin() as db:
                await fail_lost_session(db, run_id)
                run = await db.get(AgentRun, run_id)
                if run is None or run.state in TERMINAL_STATES:
                    self.lost.discard(run_id)
        run_ids = {entry.run_id for entry in self.entries.values()}
        for run_id in run_ids:
            async with self.sessions() as db:
                run = await db.get(AgentRun, run_id)
                if run is None or run.state in TERMINAL_STATES or run.state == RunState.CANCEL_REQUESTED or run.mcp_worker_id != self.worker_id:
                    close = True
                else:
                    from app.models.admin import Admin
                    from app.models.model_catalog import ModelCatalog
                    from app.models.provider_connection import ProviderConnection
                    from app.services.user_tools import load_run_tools
                    from app.services.agent_access import can_access_run_agent

                    try:
                        await load_run_tools(db, run_id, run.owner_id)
                        user = await db.get(Admin, run.owner_id)
                        close = user is None or user.status != 1 or user.is_deleted != 0
                        close = close or not await can_access_run_agent(db, run.agent_id, run.owner_id)
                        if run.model_id is not None:
                            model = await db.get(ModelCatalog, run.model_id)
                            provider = await db.get(ProviderConnection, model.provider_id) if model is not None else None
                            close = close or model is None or model.status != 1 or model.is_deleted != 0
                            close = close or provider is None or provider.status != 1 or provider.is_deleted != 0
                    except Exception:
                        close = True
            if close:
                await self.close_run(run_id, lost=True)
                async with self.sessions.begin() as db:
                    await fail_lost_session(db, run_id)
                continue
            now = time.monotonic()
            entries = [entry for entry in self.entries.values() if entry.run_id == run_id]
            expired = any(
                entry.task.done()
                or now - entry.created >= get_config().MCP_SESSION_MAX_SECONDS
                or not entry.lock.locked()
                and now - entry.touched >= get_config().MCP_SESSION_IDLE_SECONDS
                for entry in entries
            )
            if expired:
                await self.close_run(run_id, lost=True)
                async with self.sessions.begin() as db:
                    await fail_lost_session(db, run_id)
            else:
                async with self.sessions.begin() as db:
                    await db.execute(
                        update(AgentRun)
                        .where(
                            AgentRun.id == run_id,
                            AgentRun.mcp_worker_id == self.worker_id,
                            AgentRun.state.in_((RunState.RUNNING, RunState.WAITING_FOR_APPROVAL)),
                        )
                        .values(mcp_owner_expires_at=shanghai_now(naive=True) + timedelta(seconds=30))
                    )

    async def maintain(self) -> None:
        """独立于 Worker 执行循环轮询，长调用期间也核对资源状态。"""
        while True:
            try:
                async with self.sessions.begin() as db:
                    await expire_mcp_owners(db)
                await self.sweep()
            except Exception as exc:
                from loguru import logger

                logger.error("MCP 会话巡检失败：{}", type(exc).__name__)
            await asyncio.sleep(2)

    async def aclose(self) -> None:
        """进程正常退出时清理全部会话，等待中的 Run 明确结束。"""
        self.closed = True
        for run_id in {entry.run_id for entry in self.entries.values()}:
            await self.close_run(run_id, lost=True)
            with suppress(Exception):
                async with self.sessions.begin() as db:
                    await fail_lost_session(db, run_id)


def _cipher() -> Fernet:
    """回收凭单使用部署 SECRET_KEY 派生的独立用途密钥，所有 Worker 需保持一致。"""
    key = hashlib.sha256((get_config().SECRET_KEY + ":mcp-cleanup-v1").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


async def cleanup_abandoned_sessions(sessions) -> int:
    """其他 Worker 可回收失主 HTTP 会话，只做控制请求，不继承或重放业务。"""
    now = shanghai_now(naive=True)
    async with sessions() as db:
        rows = (
            await db.scalars(
                select(RunMcpResource)
                .join(AgentRun, AgentRun.id == RunMcpResource.run_id)
                .where(
                    RunMcpResource.next_attempt_at <= now,
                    or_(
                        RunMcpResource.retired.is_(True),
                        RunMcpResource.expires_at <= now,
                        AgentRun.state.in_(TERMINAL_STATES),
                        AgentRun.mcp_owner_expires_at <= now,
                    ),
                )
                .limit(8)
            )
        ).all()
    cleaned = 0
    from app.services.mcp_tools import terminate_mcp_session

    for resource in rows:
        if resource.retired or resource.expires_at <= now:
            async with sessions.begin() as db:
                await fail_lost_session(db, resource.run_id)
        succeeded = False
        try:
            payload = json.loads(_cipher().decrypt(resource.encrypted_payload.encode()))
            succeeded = await terminate_mcp_session(payload["url"], payload["credentials"], payload["session_id"], payload["protocol_version"])
        except Exception:
            pass
        async with sessions.begin() as db:
            if succeeded or resource.expires_at + timedelta(hours=24) <= now:
                if not succeeded:
                    from loguru import logger

                    logger.warning("MCP 远端会话回收已超过重试期限 run_id={} resource_id={}", resource.run_id, resource.id)
                await db.execute(delete(RunMcpResource).where(RunMcpResource.id == resource.id))
                cleaned += 1
            else:
                await db.execute(update(RunMcpResource).where(RunMcpResource.id == resource.id).values(next_attempt_at=now + timedelta(seconds=60)))
    return cleaned


async def maintain_abandoned_sessions(sessions) -> None:
    """独立回收控制循环；远端不可达时按凭单节流，不影响资源拥有者的心跳。"""
    while True:
        with suppress(Exception):
            await cleanup_abandoned_sessions(sessions)
        await asyncio.sleep(2)


async def fail_lost_session(db, run_id: str, *, expired_only: bool = False) -> None:
    """会话丢失不把 Run 重新排队；已执行写操作仍保留未知结果审计。"""
    run = await db.get(AgentRun, run_id)
    if run is not None and run.state in {RunState.RUNNING, RunState.WAITING_FOR_APPROVAL}:
        await _change_state(
            db,
            run,
            RunState.FAILED,
            now=shanghai_now(naive=True),
            failure_code=RunRuntimeFailureReason.MCP_SESSION_LOST,
            require_expired_mcp_owner=expired_only,
        )
        await db.execute(
            update(RunApproval).where(RunApproval.run_id == run_id, RunApproval.state == ApprovalState.PENDING).values(state=ApprovalState.CANCELLED)
        )


async def expire_mcp_owners(db) -> int:
    """任意健康 Worker 回收崩溃拥有者的 Run；绝不接管并重放其业务调用。"""
    ids = (
        await db.scalars(
            select(AgentRun.id)
            .where(
                AgentRun.mcp_worker_id.is_not(None),
                AgentRun.mcp_owner_expires_at <= shanghai_now(naive=True),
                AgentRun.state.in_((RunState.RUNNING, RunState.WAITING_FOR_APPROVAL)),
            )
            .limit(100)
        )
    ).all()
    for run_id in ids:
        with suppress(RunConflictError):
            await fail_lost_session(db, run_id, expired_only=True)
    return len(ids)
