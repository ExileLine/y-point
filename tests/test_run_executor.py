# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:14:31
# @Author  : yangyuexiong
# @File    : test_run_executor.py

"""验证 Run、消息与 DeepAgents 检查点的内部执行链路。"""

import asyncio
import io
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
from fastapi import UploadFile
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.tools import tool
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.artifact import Artifact
from app.models.base import Base, shanghai_now
from app.models.model_call_record import ModelCallRecord
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.cli import run_worker as run_worker_module
from app.services import run_executor as run_executor_module
from app.services import worker_tools as worker_tools_module
from app.services.artifact_store import save_upload
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
    timeout_run,
)
from app.services.run_reconciler import reconcile_expired_runs
from app.services.runtime_checkpoint import open_async_checkpointer, open_async_sqlite_checkpointer
from app.services import runtime_checkpoint


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


@tool
def failing_tool(secret: str) -> str:
    """模拟包含私有参数和异常内容的工具失败。"""
    raise RuntimeError("private-tool-error")


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


@pytest.mark.skipif(
    not os.environ.get("YPOINT_TEST_POSTGRES_CHECKPOINT_DSN"), reason="需显式提供隔离 PostgreSQL 检查点库",
)
def test_completed_run_with_postgres_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """业务 Run 在不同检查点连接上完成并继续同一线程。"""
    monkeypatch.setattr(runtime_checkpoint, "get_config", lambda: SimpleNamespace(
        CHECKPOINT_BACKEND="postgres", CHECKPOINT_POSTGRES_DSN=os.environ["YPOINT_TEST_POSTGRES_CHECKPOINT_DSN"],
    ))

    async def exercise() -> None:
        checkpoint_path = tmp_path / "unused.sqlite"
        async with open_async_checkpointer(checkpoint_path) as checkpointer:
            await checkpointer.setup()
        await exercise_completed_run(tmp_path / "runs.db", checkpoint_path)
        assert not checkpoint_path.exists()

    asyncio.run(exercise())


async def exercise_model_call_budget(database_path: Path, checkpoint_path: Path) -> None:
    """验证工具循环中的第二次模型调用被预算阻止且次数持久化。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def resolve_tools(db, run):
        """为测试运行提供无需审批的受控工具。"""
        return [echo_approved], {}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="budget-user", password="not-used", role="user", status=1, is_deleted=0))
            limited = await create_run(db, 1, input_text="调用工具后总结", max_model_calls=1)
        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "tool result"}, "id": "budget-call"}]),
            AIMessage(content="should not be returned"),
        ])
        result = await execute_text_run(
            sessions, limited.id, model, checkpoint_path, resolve_tools=resolve_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert result.state == "failed" and result.failure_code == "model_call_budget_exceeded"
        assert result.model_call_count == 1 and result.max_model_calls == 1
        async with sessions() as db:
            messages = await get_owned_messages(db, limited.id, 1)
            assert [message.role for message in messages] == ["user"]
            events = await replay_events(db, limited.id, 1)
            assert events[-1].failure_code == "model_call_budget_exceeded"

        async with sessions.begin() as db:
            allowed = await create_run(db, 1, input_text="调用工具后总结", max_model_calls=2)
        completed = await execute_text_run(
            sessions, allowed.id, ScriptedModel(responses=[
                AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "tool result"}, "id": "allowed-call"}]),
                AIMessage(content="done"),
            ]), checkpoint_path, resolve_tools=resolve_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.model_call_count == 2
    finally:
        await engine.dispose()


async def exercise_token_budget(database_path: Path, checkpoint_path: Path) -> None:
    """验证上游用量累计、超限、缺失用量和工具循环停止规则。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    async def resolve_tools(db, run):
        """向预算测试运行提供受授权工具。"""
        return [echo_approved], {}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="token-user", password="not-used", role="user", status=1, is_deleted=0))
            allowed = await create_run(db, 1, input_text="预算内回答", max_total_tokens=15)
        completed = await execute_text_run(
            sessions, allowed.id, ScriptedModel(responses=[AIMessage(content="done", usage_metadata=usage)]),
            checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.reported_total_tokens == 15
        assert completed.max_total_tokens == 15 and completed.model_call_count == 1
        async with sessions() as db:
            assert "".join(
                event.content for event in await replay_events(db, allowed.id, 1) if event.event_type == "text_delta"
            ) == "done"

        async with sessions.begin() as db:
            exceeded = await create_run(db, 1, input_text="超过预算", max_total_tokens=14)
        rejected = await execute_text_run(
            sessions, exceeded.id, ScriptedModel(responses=[AIMessage(content="private-answer", usage_metadata=usage)]),
            checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert rejected.state == "failed" and rejected.failure_code == "token_budget_exceeded"
        assert rejected.reported_total_tokens == 15 and rejected.model_call_count == 1
        async with sessions() as db:
            assert [message.role for message in await get_owned_messages(db, exceeded.id, 1)] == ["user"]
            events = await replay_events(db, exceeded.id, 1)
            assert events[-1].failure_code == "token_budget_exceeded"
            assert not any(event.event_type == "text_delta" for event in events)

        async with sessions.begin() as db:
            unknown = await create_run(db, 1, input_text="缺少用量", max_total_tokens=100)
        unavailable = await execute_text_run(
            sessions, unknown.id, ScriptedModel(responses=[AIMessage(content="answer without usage")]),
            checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert unavailable.state == "failed" and unavailable.failure_code == "token_usage_unavailable"
        assert unavailable.reported_total_tokens == 0 and unavailable.model_call_count == 1
        async with sessions() as db:
            assert not any(event.event_type == "text_delta" for event in await replay_events(db, unknown.id, 1))

        async with sessions.begin() as db:
            looping = await create_run(db, 1, input_text="工具后继续", max_total_tokens=15)
        stopped = await execute_text_run(
            sessions, looping.id, ScriptedModel(responses=[
                AIMessage(
                    content="", usage_metadata=usage,
                    tool_calls=[{"name": "echo_approved", "args": {"text": "result"}, "id": "token-tool"}],
                ),
                AIMessage(content="must not be returned", usage_metadata=usage),
            ]), checkpoint_path, resolve_tools=resolve_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert stopped.state == "failed" and stopped.failure_code == "token_budget_exceeded"
        assert stopped.reported_total_tokens == 15 and stopped.model_call_count == 1
        assert stopped.tool_call_count == 1
    finally:
        await engine.dispose()


async def exercise_cost_budget(database_path: Path, checkpoint_path: Path) -> None:
    """验证价格快照、超限、用量缺失与达到成本上限后的停止规则。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    pricing = (Decimal("2"), Decimal("4"), "USD")
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    async def resolve_tools(db, run):
        """提供无需审批的受控工具，验证后续模型调用被拦截。"""
        return [echo_approved], {}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="cost-user", password="not-used", role="user", status=1, is_deleted=0))
            db.add(ProviderConnection(
                id=7, name="cost-provider", name_key="cost-provider", kind="ollama",
                base_url="http://127.0.0.1:11434/v1", creator_id=1, timeout_seconds=30, status=1, is_deleted=0,
            ))
            db.add(ModelCatalog(
                id=7, provider_id=7, external_id="cost-model", display_name="Cost Model", creator_id=1,
                capabilities=["text"], verified_capabilities=[], input_price=pricing[0], output_price=pricing[1],
                price_currency="USD", status=1, is_deleted=0,
            ))
            allowed = await create_run(
                db, 1, model_id=7, input_text="预算内回答", max_estimated_cost=Decimal("0.000040"), cost_pricing=pricing,
            )
        async with sessions.begin() as db:
            model = await db.get(ModelCatalog, 7)
            model.input_price = Decimal("20")
            model.output_price = Decimal("40")
        completed = await execute_text_run(
            sessions, allowed.id, ScriptedModel(responses=[AIMessage(content="done", usage_metadata=usage)]),
            checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.estimated_cost == Decimal("0.000040000000")
        assert completed.cost_input_price == pricing[0] and completed.cost_output_price == pricing[1]
        assert completed.cost_currency == "USD"
        async with sessions() as db:
            events = await replay_events(db, allowed.id, 1)
            assert "".join(event.content for event in events if event.event_type == "text_delta") == "done"
            record = await db.scalar(select(ModelCallRecord).where(ModelCallRecord.run_id == allowed.id))
            assert record.estimated_cost == Decimal("0.000040000000")

        async with sessions.begin() as db:
            exceeded = await create_run(
                db, 1, model_id=7, input_text="超过预算", max_estimated_cost=Decimal("0.000039"), cost_pricing=pricing,
            )
        rejected = await execute_text_run(
            sessions, exceeded.id, ScriptedModel(responses=[AIMessage(content="private-answer", usage_metadata=usage)]),
            checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert rejected.state == "failed" and rejected.failure_code == "cost_budget_exceeded"
        assert rejected.estimated_cost == Decimal("0.000040000000") and rejected.model_call_count == 1
        async with sessions() as db:
            assert [message.role for message in await get_owned_messages(db, exceeded.id, 1)] == ["user"]
            assert not any(event.event_type == "text_delta" for event in await replay_events(db, exceeded.id, 1))

        async with sessions.begin() as db:
            unknown = await create_run(
                db, 1, model_id=7, input_text="缺少用量", max_estimated_cost=Decimal("0.000100"), cost_pricing=pricing,
            )
        unavailable = await execute_text_run(
            sessions, unknown.id, ScriptedModel(responses=[AIMessage(content="answer without usage")]),
            checkpoint_path, lease_seconds=2, poll_seconds=0.1,
        )
        assert unavailable.state == "failed" and unavailable.failure_code == "cost_usage_unavailable"
        assert unavailable.estimated_cost == 0 and unavailable.model_call_count == 1
        async with sessions() as db:
            assert not any(event.event_type == "text_delta" for event in await replay_events(db, unknown.id, 1))

        async with sessions.begin() as db:
            looping = await create_run(
                db, 1, model_id=7, input_text="工具后继续", max_estimated_cost=Decimal("0.000040"), cost_pricing=pricing,
            )
        stopped = await execute_text_run(
            sessions, looping.id, ScriptedModel(responses=[
                AIMessage(
                    content="", usage_metadata=usage,
                    tool_calls=[{"name": "echo_approved", "args": {"text": "result"}, "id": "cost-tool"}],
                ),
                AIMessage(content="must not be returned", usage_metadata=usage),
            ]), checkpoint_path, resolve_tools=resolve_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert stopped.state == "failed" and stopped.failure_code == "cost_budget_exceeded"
        assert stopped.estimated_cost == Decimal("0.000040000000")
        assert stopped.model_call_count == 1 and stopped.tool_call_count == 1
    finally:
        await engine.dispose()


async def exercise_tool_call_budget(database_path: Path, checkpoint_path: Path) -> None:
    """验证超出工具次数预算的调用不执行，并保留已开始次数。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def resolve_tools(db, run):
        """提供受授权的无审批测试工具。"""
        return [echo_approved], {}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="tool-budget-user", password="not-used", role="user", status=1, is_deleted=0))
            limited = await create_run(db, 1, input_text="调用两次工具", max_tool_calls=1)
        responses = [
            AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "first"}, "id": "tool-first"}]),
            AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "second"}, "id": "tool-second"}]),
            AIMessage(content="should not be returned"),
        ]
        rejected = await execute_text_run(
            sessions, limited.id, ScriptedModel(responses=responses), checkpoint_path,
            resolve_tools=resolve_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert rejected.state == "failed" and rejected.failure_code == "tool_call_budget_exceeded"
        assert rejected.tool_call_count == 1 and rejected.max_tool_calls == 1
        async with sessions() as db:
            assert [message.role for message in await get_owned_messages(db, limited.id, 1)] == ["user"]
            events = await replay_events(db, limited.id, 1)
            assert events[-1].failure_code == "tool_call_budget_exceeded"
            tool_events = [event for event in events if event.tool_call_id]
            assert [event.event_type for event in tool_events] == ["tool_started", "tool_completed"]
            assert tool_events[0].tool_call_id == tool_events[1].tool_call_id
            assert tool_events[0].tool_call_id != "tool-first"
            assert all(event.tool_name == "echo_approved" and event.content is None for event in tool_events)
            assert "first" not in str([event.to_dict() for event in tool_events])
            assert "second" not in str([event.to_dict() for event in tool_events])

        async with sessions.begin() as db:
            allowed = await create_run(db, 1, input_text="调用两次工具", max_tool_calls=2)
        completed = await execute_text_run(
            sessions, allowed.id, ScriptedModel(responses=[*responses[:2], AIMessage(content="done")]), checkpoint_path,
            resolve_tools=resolve_tools, lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.tool_call_count == 2
        async with sessions() as db:
            events = await replay_events(db, allowed.id, 1)
            tool_events = [event for event in events if event.tool_call_id]
            assert [event.event_type for event in tool_events] == [
                "tool_started", "tool_completed", "tool_started", "tool_completed",
            ]
            assert tool_events[0].tool_call_id == tool_events[1].tool_call_id
            assert tool_events[2].tool_call_id == tool_events[3].tool_call_id
            assert tool_events[0].tool_call_id != tool_events[2].tool_call_id

        async with sessions.begin() as db:
            broken = await create_run(db, 1, input_text="调用失败工具")

        async def resolve_failing_tool(db, run):
            """仅为失败事件测试注册受控工具。"""
            return [failing_tool], {}

        await execute_text_run(
            sessions, broken.id, ScriptedModel(responses=[
                AIMessage(content="", tool_calls=[{"name": "failing_tool", "args": {"secret": "private-input"}, "id": "failure"}]),
                AIMessage(content="done"),
            ]), checkpoint_path, resolve_tools=resolve_failing_tool, lease_seconds=2, poll_seconds=0.1,
        )
        async with sessions() as db:
            events = await replay_events(db, broken.id, 1)
            assert [event.event_type for event in events if event.tool_call_id] == ["tool_started", "tool_failed"]
            assert "private-input" not in str([event.to_dict() for event in events])
            assert "private-tool-error" not in str([event.to_dict() for event in events])
    finally:
        await engine.dispose()


async def exercise_tool_timeout(database_path: Path, checkpoint_path: Path) -> None:
    """验证单次工具超时取消执行，并留下脱敏失败事件与明确终态。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cancelled = asyncio.Event()

    @tool
    async def slow_tool(secret: str) -> str:
        """等待超时取消，不向事件公开私有参数。"""
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.set()
        return secret

    async def resolve_slow_tool(db, run):
        """向测试运行提供受授权的慢工具。"""
        return [slow_tool], {}

    async def resolve_quick_tool(db, run):
        """向测试运行提供可在时限内完成的工具。"""
        return [echo_approved], {}

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="tool-timeout-user", password="not-used", role="user", status=1, is_deleted=0))
            limited = await create_run(db, 1, input_text="调用慢工具", tool_timeout_seconds=1)
        timed_out = await execute_text_run(
            sessions, limited.id, ScriptedModel(responses=[
                AIMessage(content="", tool_calls=[{"name": "slow_tool", "args": {"secret": "private-input"}, "id": "slow-call"}]),
                AIMessage(content="should not be returned"),
            ]), checkpoint_path, resolve_tools=resolve_slow_tool, timeout_seconds=10, lease_seconds=2, poll_seconds=0.1,
        )
        assert timed_out.state == "timed_out" and timed_out.failure_code == "tool_execution_timeout"
        assert timed_out.tool_timeout_seconds == 1 and timed_out.tool_call_count == 1
        assert cancelled.is_set()
        async with sessions() as db:
            assert [message.role for message in await get_owned_messages(db, limited.id, 1)] == ["user"]
            events = await replay_events(db, limited.id, 1)
            tool_events = [event for event in events if event.tool_call_id]
            assert [event.event_type for event in tool_events] == ["tool_started", "tool_failed"]
            assert tool_events[0].tool_call_id == tool_events[1].tool_call_id
            assert events[-1].state == "timed_out" and events[-1].failure_code == "tool_execution_timeout"
            assert "private-input" not in str([event.to_dict() for event in events])

        async with sessions.begin() as db:
            quick = await create_run(db, 1, input_text="调用快工具", tool_timeout_seconds=1)
        completed = await execute_text_run(
            sessions, quick.id, ScriptedModel(responses=[
                AIMessage(content="", tool_calls=[{"name": "echo_approved", "args": {"text": "result"}, "id": "quick-call"}]),
                AIMessage(content="done"),
            ]), checkpoint_path, resolve_tools=resolve_quick_tool, timeout_seconds=10, lease_seconds=2, poll_seconds=0.1,
        )
        assert completed.state == "completed" and completed.tool_call_count == 1
    finally:
        await engine.dispose()


async def exercise_approved_resume(database_path: Path, checkpoint_path: Path) -> None:
    """验证审批后重新解析工具并从持久化检查点完成同一个 Run。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    responses = iter([
        AIMessage(content="", usage_metadata=usage, tool_calls=[
            {"name": "echo_approved", "args": {"text": "approved result"}, "id": "call-1"},
        ]),
        AIMessage(content="tool completed", usage_metadata=usage),
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
            run = await create_run(
                db, 1, input_text="请先调用工具", max_model_calls=2, max_tool_calls=1,
                max_total_tokens=30, max_estimated_cost=Decimal("0.000080"),
                cost_pricing=(Decimal("2"), Decimal("4"), "USD"),
            )
        paused = await dispatch_next_run(
            sessions, checkpoint_path, resolve_model=resolve_model, resolve_tools=resolve_tools,
            lease_seconds=2, poll_seconds=0.1,
        )
        assert paused.id == run.id and paused.state == "waiting_for_approval" and paused.checkpoint_id
        assert paused.model_call_count == 1
        assert paused.tool_call_count == 0
        assert paused.reported_total_tokens == 15 and paused.estimated_cost == Decimal("0.000040000000")
        async with sessions.begin() as db:
            assert all(event.tool_call_id is None for event in await replay_events(db, run.id, 1))
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
        assert completed.model_call_count == 2
        assert completed.tool_call_count == 1
        assert completed.reported_total_tokens == 30 and completed.estimated_cost == Decimal("0.000080000000")
        async with sessions() as db:
            messages = await get_owned_messages(db, run.id, 1)
            assert [(message.role, message.content) for message in messages] == [
                ("user", "请先调用工具"), ("assistant", "tool completed"),
            ]
            events = await replay_events(db, run.id, 1)
            assert [event.event_type for event in events if event.tool_call_id] == ["tool_started", "tool_completed"]
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


@pytest.mark.parametrize("failure", [RuntimeError, RunConflictError])
def test_heartbeat_failure_stops_running_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception],
) -> None:
    """续租失败时立即中止模型任务，不能让它脱离租约继续执行。"""

    async def failed_heartbeat(*args, **kwargs) -> None:
        raise failure("private-database-error")

    original_watch = run_executor_module._watch_run

    async def watch_after_model_starts(*args, **kwargs) -> None:
        await SlowModel.started.wait()
        await original_watch(*args, **kwargs)

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'heartbeat-failure.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        SlowModel.started = asyncio.Event()
        SlowModel.stopped = asyncio.Event()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="heartbeat-failure", password="not-used", role="user"))
                run = await create_run(db, 1, input_text="wait")
            monkeypatch.setattr(run_executor_module, "heartbeat_run", failed_heartbeat)
            monkeypatch.setattr(run_executor_module, "_watch_run", watch_after_model_starts)
            result = await asyncio.wait_for(execute_text_run(
                sessions, run.id, SlowModel(responses=[AIMessage(content="unreachable")]),
                tmp_path / "heartbeat-failure-checkpoint.db", lease_seconds=2, poll_seconds=0.05,
            ), timeout=4)
            assert SlowModel.started.is_set() and SlowModel.stopped.is_set()
            assert result.state == "failed" and result.failure_code == "lease_monitor_error"
            async with sessions() as db:
                assert [message.role for message in await get_owned_messages(db, run.id, 1)] == ["user"]
                events = await replay_events(db, run.id, 1)
                assert events[-1].failure_code == "lease_monitor_error"
                assert "private-database-error" not in str(events[-1].to_dict())
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_lease_transfer_stops_previous_model(tmp_path: Path) -> None:
    """租约变化后旧执行器停止模型，不用旧租约覆盖新的运行状态。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease-transfer.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        SlowModel.started = asyncio.Event()
        SlowModel.stopped = asyncio.Event()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="lease-transfer", password="not-used", role="user"))
                run = await create_run(db, 1, input_text="wait")
            task = asyncio.create_task(execute_text_run(
                sessions, run.id, SlowModel(responses=[AIMessage(content="unreachable")]),
                tmp_path / "lease-transfer-checkpoint.db", lease_seconds=2, poll_seconds=0.05,
            ))
            await asyncio.wait_for(SlowModel.started.wait(), timeout=3)
            async with sessions.begin() as db:
                await db.execute(update(AgentRun).where(AgentRun.id == run.id).values(lease_token="new-owner"))
            result = await asyncio.wait_for(task, timeout=4)
            assert SlowModel.stopped.is_set()
            assert result.state == "running" and result.lease_token == "new-owner"
            async with sessions() as db:
                assert [message.role for message in await get_owned_messages(db, run.id, 1)] == ["user"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_cancel_during_tool_execution(tmp_path: Path) -> None:
    """取消请求中断工具协程，执行器确认后保留可追溯的开始事件。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-tool.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        started = asyncio.Event()
        stopped = asyncio.Event()

        @tool
        async def slow_read() -> str:
            """模拟等待中的无副作用业务读取。"""
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()
            return "unreachable"

        async def resolve_tool(db, run):
            return [slow_read], {}

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="cancel-tool", password="not-used", role="user", status=1, is_deleted=0))
                run = await create_run(db, 1, input_text="取消读取")
            task = asyncio.create_task(execute_text_run(
                sessions, run.id, ScriptedModel(responses=[
                    AIMessage(content="", tool_calls=[{"name": "slow_read", "args": {}, "id": "slow-read-call"}]),
                    AIMessage(content="unreachable"),
                ]), tmp_path / "cancel-tool-checkpoint.db", resolve_tools=resolve_tool,
                lease_seconds=2, poll_seconds=0.05,
            ))
            await asyncio.wait_for(started.wait(), timeout=3)
            async with sessions.begin() as db:
                assert (await request_cancel(db, run.id, 1)).state == "cancel_requested"
            result = await asyncio.wait_for(task, timeout=4)
            assert result.state == "cancelled" and stopped.is_set()
            async with sessions() as db:
                assert [event.event_type for event in await replay_events(db, run.id, 1) if event.tool_call_id] == [
                    "tool_started",
                ]
                assert [message.role for message in await get_owned_messages(db, run.id, 1)] == ["user"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_cancel_arriving_during_timeout_finalization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """超时终态写入前出现取消请求时，执行器须确认取消并保持事件连续。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-timeout.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="cancel-timeout", password="not-used", role="user", status=1, is_deleted=0))
                run = await create_run(db, 1, input_text="long task")
                claimed = await claim_run(db, run.id)

            calls = 0

            async def raced_timeout(db, run_id, lease_token, failure_code):
                nonlocal calls
                calls += 1
                await request_cancel(db, run_id, 1)
                raise RunConflictError("取消请求赢得版本竞争")

            monkeypatch.setattr(run_executor_module, "timeout_run", raced_timeout)
            result = await run_executor_module._finish_interrupted(
                sessions, run.id, claimed.lease_token, "execution_timeout",
            )
            assert calls == 1
            assert result.state == "cancelled" and result.failure_code is None
            async with sessions() as db:
                stored = await db.get(AgentRun, run.id)
                assert stored.state == "cancelled" and stored.lease_token is None
                events = await replay_events(db, run.id, 1)
                assert [event.sequence for event in events] == [1, 2, 3, 4]
                assert [event.state for event in events] == [
                    "queued", "running", "cancel_requested", "cancelled",
                ]

            async with sessions.begin() as db:
                later_cancel = await create_run(db, 1, input_text="timeout first")
                leased = await claim_run(db, later_cancel.id)
                timed_out = await timeout_run(db, later_cancel.id, leased.lease_token)
                assert timed_out.state == "timed_out"
                assert (await request_cancel(db, later_cancel.id, 1)).state == "timed_out"
            async with sessions() as db:
                events = await replay_events(db, later_cancel.id, 1)
                assert [event.state for event in events] == ["queued", "running", "timed_out"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


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


def test_model_call_budget(tmp_path: Path) -> None:
    """运行模型调用次数上限的完整执行测试。"""
    asyncio.run(exercise_model_call_budget(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_token_budget(tmp_path: Path) -> None:
    """运行上游报告 Token 预算的完整执行测试。"""
    asyncio.run(exercise_token_budget(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_cost_budget(tmp_path: Path) -> None:
    """运行估算成本预算的完整执行测试。"""
    asyncio.run(exercise_cost_budget(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_tool_call_budget(tmp_path: Path) -> None:
    """运行工具调用次数上限的完整执行测试。"""
    asyncio.run(exercise_tool_call_budget(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


def test_tool_timeout(tmp_path: Path) -> None:
    """运行单次工具执行时限及超时终态测试。"""
    asyncio.run(exercise_tool_timeout(tmp_path / "runs.db", tmp_path / "checkpoints.db"))


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


def test_worker_expires_queued_run_before_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """独立 worker 启动后先关闭过期队列任务，不触发模型调用。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue-worker.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="已过期", max_queue_wait_seconds=1)
            async with sessions.begin() as db:
                await db.execute(update(AgentRun).where(AgentRun.id == run.id).values(
                    queue_expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
                ))
            monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
            await run_worker_module.run_worker(tmp_path / "queue-checkpoint.db", once=True)
            async with sessions() as db:
                expired = await db.get(AgentRun, run.id)
                assert expired.state == "timed_out" and expired.failure_code == "queue_timeout"
                assert expired.model_call_count == 0
                assert [event.state for event in await replay_events(db, run.id, 1)] == ["queued", "timed_out"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_worker_text_artifact_approval_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """正式 worker 按运行开关注册文件工具，并在审批恢复后执行。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifact-worker.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        storage = tmp_path / "private-artifacts"
        storage.mkdir(mode=0o700)
        checkpoint_path = tmp_path / "artifact-checkpoint.db"
        responses = None

        async def resolve_model(db, run):
            """两个 worker 进程轮次各使用一段模型响应。"""
            assert responses is not None
            return ScriptedModel(responses=[next(responses)])

        async def dispatch(sessions_arg, checkpoint_arg, **kwargs):
            """保留正式 worker 的工具解析器，仅替换上游模型。"""
            return await dispatch_next_run(sessions_arg, checkpoint_arg, resolve_model=resolve_model, **kwargs)

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="artifact-worker", password="not-used", role="user", status=1, is_deleted=0))
                artifact, _ = await save_upload(
                    UploadFile(file=io.BytesIO(b"private note"), filename="note.txt"), 1, storage,
                )
                db.add(artifact)
                run = await create_run(db, 1, input_text="读取我的文件", enable_text_artifact_tool=True)
                artifact_id = artifact.id
            responses = iter([
                AIMessage(content="", tool_calls=[{
                    "name": "read_text_artifact", "args": {"artifact_id": artifact_id}, "id": "read-call",
                }]),
                AIMessage(content="文件已读取"),
            ])
            monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
            monkeypatch.setattr(run_worker_module, "dispatch_next_run", dispatch)
            monkeypatch.setattr(worker_tools_module, "artifact_root", lambda: storage)
            await run_worker_module.run_worker(checkpoint_path, once=True)
            async with sessions.begin() as db:
                paused = await db.get(AgentRun, run.id)
                assert paused.state == "waiting_for_approval" and paused.tool_call_count == 0
                approval = await get_owned_approval(db, run.id, 1)
                assert approval.actions[0]["name"] == "read_text_artifact"
                assert approval.actions[0]["resource_id"] == artifact_id
                assert "private note" not in str(approval.actions)
                await decide_approval(db, run.id, 1, "approve")
            await run_worker_module.run_worker(checkpoint_path, once=True)
            async with sessions() as db:
                completed = await db.get(AgentRun, run.id)
                assert completed.state == "completed" and completed.tool_call_count == 1
                assert [event.event_type for event in await replay_events(db, run.id, 1) if event.tool_call_id] == [
                    "tool_started", "tool_completed",
                ]
                assert [message.content for message in await get_owned_messages(db, run.id, 1)] == [
                    "读取我的文件", "文件已读取",
                ]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_worker_lists_owned_text_artifacts_without_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """文件列表只提供元数据，仍计入工具预算并记录脱敏事件。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifact-list.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "private-artifacts"
        root.mkdir(mode=0o700)
        monkeypatch.setattr(worker_tools_module, "artifact_root", lambda: root)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="artifact-list", password="not-used", role="user", status=1, is_deleted=0))
                artifact, _ = await save_upload(
                    UploadFile(file=io.BytesIO(b"private note"), filename="note.txt"), 1, root,
                )
                db.add(artifact)
                run = await create_run(
                    db, 1, input_text="列出我的文本文件", enable_text_artifact_tool=True, max_tool_calls=1,
                )
            result = await execute_text_run(
                sessions, run.id, ScriptedModel(responses=[
                    AIMessage(content="", tool_calls=[{
                        "name": "list_text_artifacts", "args": {}, "id": "list-call",
                    }]),
                    AIMessage(content="已找到文件"),
                ]), tmp_path / "artifact-list-checkpoint.db",
                resolve_tools=worker_tools_module.make_worker_tool_resolver(sessions),
                lease_seconds=2, poll_seconds=0.1,
            )
            assert result.state == "completed" and result.tool_call_count == 1
            async with sessions() as db:
                events = await replay_events(db, run.id, 1)
                assert [(event.event_type, event.tool_name) for event in events if event.tool_call_id] == [
                    ("tool_started", "list_text_artifacts"), ("tool_completed", "list_text_artifacts"),
                ]
                assert all("private note" not in str(event.content) for event in events)
                assert (await db.get(AgentRun, run.id)).state == "completed"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_worker_publishes_answer_after_completed_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """worker 补发已完成回答的 TXT 产物，重复扫描只产生一个引用事件。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'answer-worker.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "answer-artifacts"
        root.mkdir(mode=0o700)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="answer-owner", password="not-used", role="user", status=1, is_deleted=0))
                run = await create_run(db, 1, input_text="生成回答文件", publish_answer_artifact=True)
            completed = await execute_text_run(
                sessions, run.id, ScriptedModel(responses=[AIMessage(content="最终回答")]),
                tmp_path / "answer-checkpoint.db", lease_seconds=2, poll_seconds=0.1,
            )
            assert completed.state == "completed"
            monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
            await run_worker_module.run_worker(tmp_path / "answer-checkpoint.db", once=True, artifact_path=root)
            await run_worker_module.run_worker(tmp_path / "answer-checkpoint.db", once=True, artifact_path=root)
            async with sessions() as db:
                artifacts = (await db.scalars(select(Artifact).where(Artifact.run_id == run.id))).all()
                assert len(artifacts) == 1 and artifacts[0].filename == "answer.txt"
                assert (root / artifacts[0].storage_key).read_text() == "最终回答"
                events = await replay_events(db, run.id, 1)
                assert [(event.event_type, event.artifact_id) for event in events if event.artifact_id] == [
                    ("artifact_published", artifacts[0].id),
                ]
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
