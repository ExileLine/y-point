# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:14:31
# @Author  : yangyuexiong
# @File    : test_run_executor.py

"""验证 Run、消息与 DeepAgents 检查点的内部执行链路。"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import Base, shanghai_now
from app.models.model_call_record import ModelCallRecord
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.cli import run_worker as run_worker_module
from app.services import run_executor as run_executor_module
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_executor import _approval_actions, execute_text_run
from app.services.run_manager import (
    RunConflictError,
    RunNotFoundError,
    claim_run,
    complete_expired_run,
    create_run,
    decide_approval,
    get_owned_approval,
    get_owned_messages,
    get_owned_run,
    replay_events,
    recover_expired_runs,
    request_cancel,
)
from app.services.run_reconciler import reconcile_expired_runs
from app.services.runtime_checkpoint import open_async_sqlite_checkpointer


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """允许假模型接入 DeepAgents 默认工具接口。"""
        return self


class CapturingModel(ScriptedModel):
    offered_tools: ClassVar[list[str]] = []

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """记录模型实际收到的工具列表。"""
        type(self).offered_tools = [tool.name if hasattr(tool, "name") else str(tool) for tool in tools]
        return self


class SlowModel(ScriptedModel):
    started: ClassVar[asyncio.Event]
    stopped: ClassVar[asyncio.Event]

    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        """等待取消，以验证执行器能中断正在运行的模型。"""
        type(self).started.set()
        try:
            await asyncio.sleep(30)
        finally:
            type(self).stopped.set()
        return await super()._agenerate(messages, stop, run_manager, **kwargs)


class FailingModel(ScriptedModel):
    async def _agenerate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any):
        """模拟上游失败且不泄露原始异常正文。"""
        raise RuntimeError("private-upstream-error")


class SimulatedProcessCrash(BaseException):
    """模拟检查点完成后、业务事务提交前进程退出。"""


@tool
def echo_approved(text: str) -> str:
    """返回已批准的测试工具参数。"""
    return text


async def exercise_completed_run(database_path: Path, checkpoint_path: Path) -> None:
    """验证用户输入、检查点与最终回答形成可读取的持久化结果。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="completed-user", password="not-used", role="user", status=1, is_deleted=0))
            run = await create_run(db, 1, request_key="hello", input_text="say hello")
            duplicate = await create_run(db, 1, request_key="hello", input_text="say hello")
            assert duplicate.id == run.id
            with pytest.raises(RunConflictError):
                await create_run(db, 1, request_key="hello", input_text="different")
        completed = await execute_text_run(
            sessions, run.id, ScriptedModel(responses=[AIMessage(content="hello back")]), checkpoint_path,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.checkpoint_id
        async with sessions.begin() as db:
            messages = await get_owned_messages(db, run.id, 1)
            assert [(message.role, message.content) for message in messages] == [
                ("user", "say hello"), ("assistant", "hello back"),
            ]
            events = await replay_events(db, run.id, 1)
            assert [(event.event_type, event.state) for event in events] == [
                ("state", "queued"), ("state", "running"), ("text_delta", "running"), ("state", "completed"),
            ]
            assert "".join(event.content or "" for event in events) == "hello back"
            assert all("say hello" not in str(event.to_dict()) for event in events)
            with pytest.raises(RunNotFoundError, match="运行不存在"):
                await get_owned_messages(db, run.id, 2)
            followup = await create_run(db, 1, thread_id=run.thread_id, input_text="again")
        next_result = await execute_text_run(
            sessions, followup.id, ScriptedModel(responses=[AIMessage(content="second answer")]), checkpoint_path,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert next_result.state == "completed" and next_result.checkpoint_id != completed.checkpoint_id
    finally:
        await engine.dispose()


async def exercise_approved_resume(database_path: Path, checkpoint_path: Path) -> None:
    """验证审批后重新解析工具并从持久化检查点完成同一个 Run。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    responses = iter([
        AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "approved result"}, "id": "call-1"}]),
        AIMessage(content="tool completed"),
        AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "must not run"}, "id": "call-2"}]),
        AIMessage(content="must not appear"),
    ])

    async def resolve_model(db, run):
        """每次独立调度使用一段预置模型响应。"""
        return ScriptedModel(responses=[next(responses)])

    async def resolve_tools(db, run):
        """模拟恢复前再次授权同一个需审批的工具。"""
        user = await db.get(Admin, run.owner_id)
        if user is None or user.status != 1:
            raise RuntimeError("用户不可用")
        return [echo_approved], {"echo_approved": True}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="approval-user", password="not-used", role="user", status=1, is_deleted=0))
            run = await create_run(db, 1, input_text="请先调用工具")
        paused = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert paused.id == run.id and paused.state == "waiting_for_approval" and paused.checkpoint_id
        async with sessions.begin() as db:
            approval = await get_owned_approval(db, run.id, 1)
            assert approval.state == "pending" and approval.actions[0]["name"] == "echo_approved"
            assert "approved result" not in str(approval.actions)
            await decide_approval(db, run.id, 1, "approve")
        assert await dispatch_next_run(sessions, checkpoint_path, resolve_model=resolve_model) is None
        completed = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.id == run.id and completed.state == "completed"
        async with sessions() as db:
            messages = await get_owned_messages(db, run.id, 1)
            assert [(message.role, message.content) for message in messages] == [
                ("user", "请先调用工具"), ("assistant", "tool completed"),
            ]
            events = await replay_events(db, run.id, 1)
            assert [event.state for event in events if event.event_type == "state"] == [
                "queued", "running", "waiting_for_approval", "running", "completed",
            ]
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="unused")]), tools=[echo_approved],
                interrupt_on={"echo_approved": True}, checkpointer=checkpointer,
            )
            snapshot = await agent.aget_state({"configurable": {"thread_id": run.thread_id}})
            assert any(isinstance(message, ToolMessage) and message.content == "approved result"
                       for message in snapshot.values["messages"])

        async with sessions.begin() as db:
            revoked = await create_run(db, 1, input_text="再次调用工具")
        paused_again = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert paused_again.id == revoked.id and paused_again.state == "waiting_for_approval"
        async with sessions.begin() as db:
            await decide_approval(db, revoked.id, 1, "approve")

        async def revoked_tools(db, run):
            """模拟审批后工具权限被撤销。"""
            return [], {}

        denied = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=revoked_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert denied.id == revoked.id and denied.state == "failed" and denied.failure_code == "runtime_error"
        async with sessions() as db:
            assert [message.role for message in await get_owned_messages(db, revoked.id, 1)] == ["user"]
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="unused")]), tools=[echo_approved],
                interrupt_on={"echo_approved": True}, checkpointer=checkpointer,
            )
            snapshot = await agent.aget_state({"configurable": {"thread_id": revoked.thread_id}})
            assert snapshot.next and not any(isinstance(message, ToolMessage) for message in snapshot.values["messages"])
    finally:
        await engine.dispose()


async def exercise_repeated_approvals(database_path: Path, checkpoint_path: Path) -> None:
    """验证同一运行的两次工具调用分别等待独立审批。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    responses = iter([
        AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "first"}, "id": "call-1"}]),
        AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "second"}, "id": "call-2"}]),
        AIMessage(content="both completed"),
    ])

    async def resolve_model(db, run):
        """每次调度返回下一段预置模型响应。"""
        return ScriptedModel(responses=[next(responses)])

    async def resolve_tools(db, run):
        """为每次恢复重新提供需审批的测试工具。"""
        return [echo_approved], {"echo_approved": True}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="repeat-approval", password="not-used", role="user", status=1, is_deleted=0))
            run = await create_run(db, 1, input_text="连续调用两次工具")
        first = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert first.state == "waiting_for_approval"
        async with sessions.begin() as db:
            first_approval = await get_owned_approval(db, run.id, 1)
            await decide_approval(db, run.id, 1, "approve")
        second = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert second.state == "waiting_for_approval" and second.checkpoint_id != first.checkpoint_id
        async with sessions.begin() as db:
            second_approval = await get_owned_approval(db, run.id, 1)
            assert second_approval.id != first_approval.id and second_approval.actions != first_approval.actions
            assert second_approval.state == "pending"
            await decide_approval(db, run.id, 1, "approve")
        completed = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed"
        async with sessions() as db:
            assert [event.state for event in await replay_events(db, run.id, 1) if event.event_type == "state"] == [
                "queued", "running", "waiting_for_approval", "running", "waiting_for_approval", "running", "completed",
            ]
            assert (await get_owned_messages(db, run.id, 1))[-1].content == "both completed"
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="unused")]), tools=[echo_approved],
                interrupt_on={"echo_approved": True}, checkpointer=checkpointer,
            )
            snapshot = await agent.aget_state({"configurable": {"thread_id": run.thread_id}})
            assert [message.content for message in snapshot.values["messages"] if isinstance(message, ToolMessage)] == [
                "first", "second",
            ]
    finally:
        await engine.dispose()


async def exercise_changed_approval(database_path: Path, checkpoint_path: Path) -> None:
    """验证审批动作摘要变化后执行器拒绝恢复工具。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def resolve_model(db, run):
        """提供单次需审批的工具调用。"""
        return ScriptedModel(responses=[AIMessage(
            content="", tool_calls=[{"name": "echo_approved", "args": {"text": "private"}, "id": "call-1"}],
        )])

    async def resolve_tools(db, run):
        """提供恢复前仍可使用的同名工具。"""
        return [echo_approved], {"echo_approved": True}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="changed-approval", password="not-used", role="user", status=1, is_deleted=0))
            run = await create_run(db, 1, input_text="调用工具")
        paused = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert paused.state == "waiting_for_approval"
        async with sessions.begin() as db:
            approval = await decide_approval(db, run.id, 1, "approve")
            approval.actions = [{"name": "echo_approved", "arguments_hash": "0" * 64}]
        failed = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert failed.state == "failed" and failed.failure_code == "checkpoint_mismatch"
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="unused")]), tools=[echo_approved],
                interrupt_on={"echo_approved": True}, checkpointer=checkpointer,
            )
            snapshot = await agent.aget_state({"configurable": {"thread_id": run.thread_id}})
            assert snapshot.next and not any(isinstance(message, ToolMessage) for message in snapshot.values["messages"])
    finally:
        await engine.dispose()


async def exercise_checkpoint_reconciliation(
    database_path: Path, checkpoint_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 worker 只从当前 Run 的完整检查点补写最终回答。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def crash_before_commit(*args, **kwargs):
        """模拟进程在业务完成事务开始前崩溃。"""
        raise SimulatedProcessCrash()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="reconcile-user", password="not-used", role="user", status=1, is_deleted=0))
            run = await create_run(db, 1, input_text="请回答")
        with monkeypatch.context() as patcher:
            patcher.setattr(run_executor_module, "complete_run", crash_before_commit)
            with pytest.raises(SimulatedProcessCrash):
                await execute_text_run(
                    sessions, run.id, ScriptedModel(responses=[AIMessage(content="已完成的回答")]), checkpoint_path,
                    lease_seconds=2, poll_seconds=0.1,
                )
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="unused")]), tools=[], checkpointer=checkpointer,
            )
            snapshot = await agent.aget_state({"configurable": {"thread_id": run.thread_id}})
            assert snapshot.metadata.get("tool_count") == 0, snapshot.metadata
        async with sessions.begin() as db:
            interrupted = await db.get(AgentRun, run.id)
            assert interrupted.state == "running" and interrupted.checkpoint_id is None
            with pytest.raises(RunConflictError):
                await complete_expired_run(db, run.id, checkpoint_id="untrusted", final_text="untrusted")
            await db.execute(update(AgentRun).where(AgentRun.id == run.id).values(
                lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
            ))
        monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
        await run_worker_module.run_worker(checkpoint_path, once=True)
        async with sessions() as db:
            completed = await db.get(AgentRun, run.id)
            assert completed.state == "completed" and completed.checkpoint_id
            assert [message.content for message in await get_owned_messages(db, run.id, 1)] == ["请回答", "已完成的回答"]
            assert [event.state for event in await replay_events(db, run.id, 1) if event.event_type == "state"] == [
                "queued", "running", "completed",
            ]
            followup = completed.thread_id
        assert await reconcile_expired_runs(sessions, checkpoint_path) == 0
        async with sessions.begin() as db:
            next_run = await create_run(db, 1, thread_id=followup, input_text="继续")
        next_result = await execute_text_run(
            sessions, next_run.id, ScriptedModel(responses=[AIMessage(content="第二轮回答")]), checkpoint_path,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert next_result.state == "completed"

        async with sessions.begin() as db:
            foreign = await create_run(db, 1, input_text="检查归属")
            await claim_run(db, foreign.id)
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="不属于该运行")]), tools=[], checkpointer=checkpointer,
            )
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "检查归属"}]},
                {"configurable": {"thread_id": foreign.thread_id}, "metadata": {"run_id": "another-run"}},
            )
        async with sessions.begin() as db:
            await db.execute(update(AgentRun).where(AgentRun.id == foreign.id).values(
                lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
            ))
        assert await reconcile_expired_runs(sessions, checkpoint_path) == 0
        async with sessions.begin() as db:
            assert await recover_expired_runs(db) == 1
        async with sessions() as db:
            rejected = await db.get(AgentRun, foreign.id)
            assert rejected.state == "failed" and rejected.failure_code == "lease_expired"
            assert [message.role for message in await get_owned_messages(db, foreign.id, 1)] == ["user"]

        async with sessions.begin() as db:
            legacy = await create_run(db, 1, input_text="旧版检查点")
            await claim_run(db, legacy.id)
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="旧版回答")]), tools=[], checkpointer=checkpointer,
            )
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "旧版检查点"}]},
                {"configurable": {"thread_id": legacy.thread_id}, "metadata": {"run_id": legacy.id}},
            )
        async with sessions.begin() as db:
            await db.execute(update(AgentRun).where(AgentRun.id == legacy.id).values(
                lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
            ))
        assert await reconcile_expired_runs(sessions, checkpoint_path) == 0
        async with sessions.begin() as db:
            assert await recover_expired_runs(db) == 1
        async with sessions() as db:
            old_run = await db.get(AgentRun, legacy.id)
            assert old_run.state == "failed" and old_run.failure_code == "lease_expired"

        async with sessions.begin() as db:
            pending = await create_run(db, 1, input_text="审批工具")
            await claim_run(db, pending.id)
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(
                    content="", tool_calls=[{"name": "echo_approved", "args": {"text": "pending"}, "id": "call-pending"}],
                )]), tools=[echo_approved], interrupt_on={"echo_approved": True}, checkpointer=checkpointer,
            )
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "审批工具"}]},
                {"configurable": {"thread_id": pending.thread_id}, "metadata": {"run_id": pending.id, "tool_count": 0}},
            )
        async with sessions.begin() as db:
            await db.execute(update(AgentRun).where(AgentRun.id == pending.id).values(
                lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
            ))
        assert await reconcile_expired_runs(sessions, checkpoint_path) == 0
        async with sessions.begin() as db:
            assert await recover_expired_runs(db) == 1
        async with sessions() as db:
            blocked = await db.get(AgentRun, pending.id)
            assert blocked.state == "failed" and blocked.failure_code == "lease_expired"

        async with sessions.begin() as db:
            tool_run = await create_run(db, 1, input_text="调用工具后回答")
            await claim_run(db, tool_run.id)
        async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[
                    AIMessage(content="", tool_calls=[
                        {"name": "echo_approved", "args": {"text": "tool-result"}, "id": "call-complete"},
                    ]),
                    AIMessage(content="工具执行完成"),
                ]), tools=[echo_approved], checkpointer=checkpointer,
            )
            await agent.ainvoke(
                {"messages": [{"role": "user", "content": "调用工具后回答"}]},
                {"configurable": {"thread_id": tool_run.thread_id},
                 "metadata": {"run_id": tool_run.id, "tool_count": 1}},
            )
        async with sessions.begin() as db:
            await db.execute(update(AgentRun).where(AgentRun.id == tool_run.id).values(
                lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
            ))
        assert await reconcile_expired_runs(sessions, checkpoint_path) == 0
        async with sessions.begin() as db:
            assert await recover_expired_runs(db) == 1
        async with sessions() as db:
            excluded = await db.get(AgentRun, tool_run.id)
            assert excluded.state == "failed" and excluded.failure_code == "lease_expired"
            assert [message.role for message in await get_owned_messages(db, tool_run.id, 1)] == ["user"]
    finally:
        await engine.dispose()


async def exercise_builtin_tool_denied(database_path: Path, checkpoint_path: Path) -> None:
    """验证未显式授权时内置工具不可见且无法执行。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="no-tools-user", password="not-used", role="user", status=1, is_deleted=0))
            normal = await create_run(db, 1, input_text="回答即可")
        CapturingModel.offered_tools = []
        completed = await execute_text_run(
            sessions, normal.id, CapturingModel(responses=[AIMessage(content="ok")]), checkpoint_path,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and CapturingModel.offered_tools == []
        async with sessions.begin() as db:
            unauthorized = await create_run(db, 1, input_text="调用内置文件工具")
        denied = await execute_text_run(
            sessions, unauthorized.id, ScriptedModel(responses=[AIMessage(
                content="", tool_calls=[{"name": "write_file", "args": {"file_path": "/private", "content": "secret"}, "id": "call-1"}],
            )]), checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert denied.state == "failed" and denied.failure_code == "model_error"
        async with sessions() as db:
            assert [message.role for message in await get_owned_messages(db, unauthorized.id, 1)] == ["user"]
        async with sessions.begin() as db:
            collision = await create_run(db, 1, input_text="测试工具重名")

        async def colliding_tools(db, run):
            """模拟业务工具占用 DeepAgents 内置名称。"""
            return [echo_approved.model_copy(update={"name": "write_file"})], {}

        rejected = await execute_text_run(
            sessions, collision.id, ScriptedModel(responses=[AIMessage(content="must not run")]), checkpoint_path,
            resolve_tools=colliding_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert rejected.state == "failed" and rejected.failure_code == "runtime_error"
    finally:
        await engine.dispose()


async def exercise_cancel_and_timeout(database_path: Path, checkpoint_path: Path) -> None:
    """验证取消信号和执行时限都能停止模型并记录终态。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="runtime-user", password="not-used", role="user", status=1, is_deleted=0))
        for action in ("cancel", "timeout", "disable"):
            SlowModel.started = asyncio.Event()
            SlowModel.stopped = asyncio.Event()
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="wait", model_timeout_seconds=1 if action == "timeout" else 120)
            task = asyncio.create_task(execute_text_run(
                sessions, run.id, SlowModel(responses=[AIMessage(content="unused")]), checkpoint_path,
                timeout_seconds=5, lease_seconds=2, poll_seconds=0.05,
            ))
            await asyncio.wait_for(SlowModel.started.wait(), timeout=3)
            if action == "cancel":
                async with sessions.begin() as db:
                    assert (await request_cancel(db, run.id, 1)).state == "cancel_requested"
            elif action == "disable":
                async with sessions.begin() as db:
                    (await db.get(Admin, 1)).status = 0
            result = await asyncio.wait_for(task, timeout=4)
            assert result.state == ("timed_out" if action == "timeout" else "cancelled")
            assert SlowModel.stopped.is_set()
            async with sessions.begin() as db:
                assert [message.role for message in await get_owned_messages(db, run.id, 1)] == ["user"]
                if action == "disable":
                    (await db.get(Admin, 1)).status = 1
    finally:
        await engine.dispose()


async def exercise_failed_model(database_path: Path, checkpoint_path: Path) -> None:
    """验证模型失败只留下分类，不将上游异常存入业务表。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="failed-user", password="not-used", role="user", status=1, is_deleted=0))
            run = await create_run(db, 1, input_text="fail")
        result = await execute_text_run(sessions, run.id, FailingModel(responses=[]), checkpoint_path, lease_seconds=2, poll_seconds=0.1)
        assert result.state == "failed" and result.failure_code == "model_error"
        async with sessions.begin() as db:
            assert (await get_owned_run(db, run.id, 1)).failure_code == "model_error"
            assert all("private-upstream-error" not in str(event.to_dict()) for event in await replay_events(db, run.id, 1))
    finally:
        await engine.dispose()


async def exercise_runtime_call_records(database_path: Path, checkpoint_path: Path) -> None:
    """验证跨轮用量分别计费，失败调用只保存脱敏结果。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="metered-user", password="not-used", role="user", status=1, is_deleted=0))
            db.add(ProviderConnection(
                id=7, name="metered-provider", name_key="metered-provider", kind="ollama",
                base_url="http://127.0.0.1:11434/v1", creator_id=1, timeout_seconds=30, status=1, is_deleted=0,
            ))
            db.add(ModelCatalog(
                id=7, provider_id=7, external_id="metered-model", display_name="Metered Model", creator_id=1,
                capabilities=["text"], verified_capabilities=[], input_price=Decimal("2"), output_price=Decimal("4"),
                price_currency="USD", status=1, is_deleted=0,
            ))
            first = await create_run(db, 1, model_id=7, input_text="first")
        first_result = await execute_text_run(
            sessions, first.id, ScriptedModel(responses=[AIMessage(
                content="first answer", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )]), checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert first_result.state == "completed"
        async with sessions.begin() as db:
            second = await create_run(db, 1, model_id=7, thread_id=first.thread_id, input_text="second")
        second_result = await execute_text_run(
            sessions, second.id, ScriptedModel(responses=[AIMessage(
                content="second answer", usage_metadata={"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
            )]), checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert second_result.state == "completed"
        async with sessions.begin() as db:
            failed = await create_run(db, 1, model_id=7, input_text="fail")
        failed_result = await execute_text_run(
            sessions, failed.id, FailingModel(responses=[]), checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert failed_result.state == "failed"
        async with sessions() as db:
            records = (await db.scalars(select(ModelCallRecord).order_by(ModelCallRecord.id))).all()
            assert [record.run_id for record in records] == [first.id, second.id, failed.id]
            assert [(record.input_tokens, record.output_tokens) for record in records] == [(10, 5), (20, 8), (None, None)]
            assert records[0].estimated_cost == Decimal("0.000040000000")
            assert records[1].estimated_cost == Decimal("0.000072000000")
            assert records[2].available is False and records[2].failure == "model_error"
            assert records[2].estimated_cost is None and all(record.call_type == "runtime_run" for record in records)
            assert all("private-upstream-error" not in str(record.__dict__) for record in records)
    finally:
        await engine.dispose()


async def exercise_checkpoint_mismatch(database_path: Path, checkpoint_path: Path, *, remove_file: bool) -> None:
    """验证缺失或意外前进的检查点不会成为下一轮模型上下文。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="checkpoint-user", password="not-used", role="user", status=1, is_deleted=0))
            first = await create_run(db, 1, input_text="first")
        completed = await execute_text_run(
            sessions, first.id, ScriptedModel(responses=[AIMessage(content="first answer")]), checkpoint_path,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.checkpoint_id
        if remove_file:
            checkpoint_path.unlink()
        else:
            async with open_async_sqlite_checkpointer(checkpoint_path) as checkpointer:
                agent = create_deep_agent(
                    model=ScriptedModel(responses=[AIMessage(content="unrecorded answer")]), tools=[], checkpointer=checkpointer,
                )
                config = {"configurable": {"thread_id": first.thread_id}}
                await agent.ainvoke({"messages": [{"role": "user", "content": "unrecorded input"}]}, config)
                assert (await agent.aget_state(config)).config["configurable"]["checkpoint_id"] != completed.checkpoint_id
        async with sessions.begin() as db:
            followup = await create_run(db, 1, thread_id=first.thread_id, input_text="second")
        result = await execute_text_run(
            sessions, followup.id, ScriptedModel(responses=[AIMessage(content="should not appear")]), checkpoint_path,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert result.state == "failed" and result.failure_code == "checkpoint_mismatch"
        async with sessions.begin() as db:
            assert [message.role for message in await get_owned_messages(db, followup.id, 1)] == ["user"]
            assert [(event.event_type, event.state) for event in await replay_events(db, followup.id, 1)] == [
                ("state", "queued"), ("state", "running"), ("state", "failed"),
            ]
    finally:
        await engine.dispose()


def test_completed_run(tmp_path: Path) -> None:
    """运行持久化执行和多轮线程测试。"""
    asyncio.run(exercise_completed_run(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_approved_resume(tmp_path: Path) -> None:
    """运行审批后跨调度检查点恢复测试。"""
    asyncio.run(exercise_approved_resume(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_repeated_approvals(tmp_path: Path) -> None:
    """运行同一 Run 连续审批两次的测试。"""
    asyncio.run(exercise_repeated_approvals(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_changed_approval(tmp_path: Path) -> None:
    """运行审批动作摘要不一致时的拒绝测试。"""
    asyncio.run(exercise_changed_approval(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_checkpoint_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行检查点完成后业务提交失败的自动补偿测试。"""
    asyncio.run(exercise_checkpoint_reconciliation(tmp_path / "runs.db", tmp_path / "checkpoints.db", monkeypatch))


def test_builtin_tool_denied(tmp_path: Path) -> None:
    """运行 DeepAgents 内置工具授权边界测试。"""
    asyncio.run(exercise_builtin_tool_denied(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_cancel_and_timeout(tmp_path: Path) -> None:
    """运行模型取消与超时测试。"""
    asyncio.run(exercise_cancel_and_timeout(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_model_failure(tmp_path: Path) -> None:
    """运行模型故障脱敏测试。"""
    asyncio.run(exercise_failed_model(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_runtime_call_records(tmp_path: Path) -> None:
    """运行运行时模型调用记录和跨轮用量测试。"""
    asyncio.run(exercise_runtime_call_records(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


@pytest.mark.parametrize("remove_file", [False, True])
def test_checkpoint_mismatch(tmp_path: Path, remove_file: bool) -> None:
    """运行检查点意外前进和文件丢失的边界测试。"""
    asyncio.run(exercise_checkpoint_mismatch(tmp_path / "runs.db", tmp_path / "checkpoints.db", remove_file=remove_file))


def test_worker_rejects_unusable_checkpoint_before_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """无效检查点路径必须在执行器领取排队运行前暴露。"""
    blocker = tmp_path / "not-a-directory"
    blocker.touch()
    dispatch = AsyncMock(return_value=None)

    @asynccontextmanager
    async def fake_session():
        """避免回归测试接触真实业务数据库。"""
        yield object()

    monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", SimpleNamespace(begin=fake_session))
    monkeypatch.setattr(run_worker_module, "recover_expired_runs", AsyncMock(return_value=0))
    monkeypatch.setattr(run_worker_module, "expire_approvals", AsyncMock(return_value=0))
    monkeypatch.setattr(run_worker_module, "dispatch_next_run", dispatch)
    with pytest.raises(OSError):
        asyncio.run(run_worker_module.run_worker(blocker / "checkpoint.sqlite", once=True))
    dispatch.assert_not_awaited()


def test_approval_actions_hide_arguments() -> None:
    """验证审批事件只提取工具名和参数摘要，不保存原始秘密。"""
    snapshot = SimpleNamespace(tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={
        "action_requests": [{"name": "search", "args": {"token": "private-secret"}, "description": "private-secret"}],
    })])])
    actions = _approval_actions(snapshot)
    assert len(actions) == 1 and actions[0]["name"] == "search"
    assert len(actions[0]["arguments_hash"]) == 64
    assert "private-secret" not in str(actions)


def test_dispatcher_rejects_missing_user(tmp_path: Path) -> None:
    """验证独立队列扫描在用户不存在时终止任务，不调用模型。"""

    async def exercise() -> None:
        """在临时数据库中调用默认模型解析流程。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1, model_id=1, input_text="hello")
            failed = await dispatch_next_run(sessions, tmp_path / "checkpoints.db", lease_seconds=2, poll_seconds=0.1)
            assert failed.id == run.id and failed.state == "failed" and failed.failure_code == "runtime_error"
            assert await dispatch_next_run(sessions, tmp_path / "checkpoints.db", lease_seconds=2, poll_seconds=0.1) is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_OLLAMA_M3_RUN") != "1", reason="仅在显式启用本机 Ollama 执行验收时运行")
def test_live_ollama_persisted_run(tmp_path: Path) -> None:
    """用本机真实模型验证运行领取、检查点和最终回答入库。"""

    async def exercise_live_run() -> None:
        """在临时业务库中执行一次完整的真实文本运行。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="m3-live", password="not-used", role="user", status=1, is_deleted=0))
                db.add(ProviderConnection(
                    id=999004, name="m3-ollama", name_key="m3-ollama", kind="ollama",
                    base_url="http://127.0.0.1:11434/v1", creator_id=1, encrypted_credentials=None,
                    timeout_seconds=60, status=1, is_deleted=0,
                ))
                db.add(ModelCatalog(
                    id=999004, provider_id=999004, external_id="gemma4:31b-cloud", display_name="Gemma4",
                    creator_id=1, capabilities=["text"], verified_capabilities=[], status=1, is_deleted=0,
                ))
                run = await create_run(db, 1, input_text="请用一句中文回复：你好。", model_id=999004)
            completed = await dispatch_next_run(
                sessions, tmp_path / "checkpoints.db", timeout_seconds=60, lease_seconds=30, poll_seconds=2,
            )
            assert completed.state == "completed" and completed.checkpoint_id
            async with sessions.begin() as db:
                messages = await get_owned_messages(db, run.id, 1)
                assert len(messages) == 2 and messages[-1].role == "assistant" and messages[-1].content.strip()
                events = await replay_events(db, run.id, 1)
                assert any(event.event_type == "text_delta" for event in events)
                assert "".join(event.content or "" for event in events) == messages[-1].content
        finally:
            await engine.dispose()

    asyncio.run(exercise_live_run())
