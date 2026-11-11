# -*- coding: utf-8 -*-
# @Time    : 2026-10-27 21:03:14
# @Author  : yangyuexiong
# @File    : test_long_context.py

"""验证小窗口模型的长会话摘要边界。"""

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import RunModelFailureReason, RunState, RunTimeoutReason
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.run_executor import execute_text_run
from app.services.model_adapter import build_chat_model
from app.services.run_manager import create_run
from app.services.runtime_adapter import RuntimeRunConfig


class TrackingModel(FakeMessagesListChatModel):
    summary_calls: ClassVar[int] = 0
    normal_calls: ClassVar[int] = 0
    fail_summary: ClassVar[bool] = False

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """兼容运行时工具绑定接口，并返回当前假模型。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """生成可计数的假模型回复，并按测试配置模拟摘要失败。"""
        if any("压缩成准确的上下文摘要" in str(message.content) for message in messages):
            type(self).summary_calls += 1
            if type(self).fail_summary:
                raise RuntimeError("private-summary-error")
            answer = "此前的用户输入已压缩为摘要。"
        else:
            type(self).normal_calls += 1
            answer = "已收到"
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content=answer,
                        usage_metadata={
                            "input_tokens": 20,
                            "output_tokens": 5,
                            "total_tokens": 25,
                        },
                    ),
                ),
            ],
        )


class ToolHistoryModel(FakeMessagesListChatModel):
    """先调用采购简报工具，再根据检查点中的事实回答。"""

    normal_calls: ClassVar[int] = 0
    summary_calls: ClassVar[int] = 0
    fail_summary: ClassVar[bool] = False
    summary_prompts: ClassVar[list[str]] = []

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """兼容运行时工具绑定并保留预置调用行为。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """在摘要调用中记录输入，并在普通调用中读取或回答项目事实。"""
        content = "\n".join(str(message.content) for message in messages)
        if "压缩成准确的上下文摘要" in content:
            type(self).summary_calls += 1
            type(self).summary_prompts.append(content)
            if type(self).fail_summary:
                raise RuntimeError("private-summary-error-after-tool")
            answer = AIMessage(content="已完成采购简报读取；项目编号星河-27，预算18万元，交付日期2026-12-15。")
        else:
            type(self).normal_calls += 1
            if type(self).normal_calls == 1:
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_project_brief",
                            "args": {},
                            "id": "read-project-brief",
                        },
                    ],
                )
            else:
                answer = AIMessage(content="星河-27" if "星河-27" in content else "未找到项目编号")
        return ChatResult(generations=[ChatGeneration(message=answer)])


@pytest.mark.parametrize("failure_after_tool", [False, True])
def test_tool_history_summary_respects_checkpoint_boundary(tmp_path: Path, failure_after_tool: bool) -> None:
    """工具结果经摘要仍可续聊，摘要失败后不重放已执行工具。"""

    async def exercise() -> None:
        """在隔离业务库和检查点中验证工具历史及失败边界。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tool-history.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "tool-history-checkpoint.db"
        ToolHistoryModel.normal_calls = ToolHistoryModel.summary_calls = 0
        ToolHistoryModel.fail_summary = failure_after_tool
        ToolHistoryModel.summary_prompts = []
        tool_invocations = 0
        facts = "采购项目星河-27，预算18万元，交付日期2026-12-15，负责人林青。"

        @tool
        def read_project_brief() -> str:
            """读取当前测试会话的采购项目简报。"""
            nonlocal tool_invocations
            tool_invocations += 1
            return facts + ("补充条款：按期交付。" * 300 if failure_after_tool else "")

        async def resolve_tools(db, run):
            """仅为当前运行提供受控的只读简报工具。"""
            return [read_project_brief], {}

        snapshot = dict(
            agent_id=1,
            model_id=1,
            session_id="a" * 36,
            thread_id="b" * 36,
            agent_config_version=1,
            agent_system_prompt="准确保留采购项目编号。",
            agent_temperature=Decimal("0"),
            agent_max_output_tokens=256,
            max_model_calls=5,
        )
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="tool-summary-owner", password="unused", role="user", status=1, is_deleted=0),
                        AgentConfig(
                            id=1,
                            owner_id=1,
                            name="采购助手",
                            default_model_id=1,
                            temperature=Decimal("0"),
                            max_output_tokens=256,
                            enabled=True,
                            version=1,
                            is_deleted=0,
                            status=1,
                        ),
                        ProviderConnection(
                            id=1,
                            name="tool-summary-provider",
                            name_key="tool-summary-provider",
                            kind="ollama",
                            base_url="http://localhost:11434/v1",
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=1,
                            provider_id=1,
                            external_id="small-context",
                            display_name="Small context",
                            verified_capabilities=["text", "tools"],
                            context_window=2048,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        AgentSession(id=snapshot["session_id"], owner_id=1, agent_id=1, thread_id=snapshot["thread_id"], title="采购简报"),
                    ],
                )
            model = ToolHistoryModel(responses=[AIMessage(content="fallback")])
            first_input = "请读取采购简报并记住项目编号。"
            async with sessions.begin() as db:
                first = await create_run(db, 1, input_text=first_input, **snapshot)
            first_result = await execute_text_run(sessions, first.id, model, checkpoint, resolve_tools=resolve_tools)
            assert tool_invocations == 1 and first_result.tool_call_count == 1
            if failure_after_tool:
                assert first_result.state == "failed" and first_result.failure_code == "summary_error"
                assert ToolHistoryModel.summary_calls >= 1
                async with sessions() as db:
                    messages = (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.run_id == first.id))).all()
                    assert [message.role for message in messages] == ["user"]
                async with sessions.begin() as db:
                    followup = await create_run(db, 1, input_text="继续回答项目编号", **snapshot)
                blocked = await execute_text_run(sessions, followup.id, model, checkpoint, resolve_tools=resolve_tools)
                assert blocked.state == "failed" and blocked.failure_code == "checkpoint_mismatch"
                assert tool_invocations == 1
            else:
                assert first_result.state == "completed", first_result.failure_code
                second_input = "请只回答采购项目编号。以下是本轮填充资料：" + "甲乙丙丁" * 250
                async with sessions.begin() as db:
                    second = await create_run(db, 1, input_text=second_input, **snapshot)
                resumed = await execute_text_run(sessions, second.id, model, checkpoint, resolve_tools=resolve_tools)
                assert resumed.state == "completed", resumed.failure_code
                assert ToolHistoryModel.summary_calls >= 1
                assert any("星河-27" in prompt for prompt in ToolHistoryModel.summary_prompts)
                async with sessions() as db:
                    messages = (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))).all()
                    assert [message.content for message in messages if message.role == "user"] == [first_input, second_input]
                    assert [message.content for message in messages if message.role == "assistant"] == ["星河-27", "星河-27"]
        finally:
            ToolHistoryModel.fail_summary = False
            await engine.dispose()

    asyncio.run(exercise())


def test_long_session_summarizes_without_changing_visible_messages(tmp_path: Path) -> None:
    """验证长会话触发摘要且保留原始业务消息。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：验证长会话触发摘要且保留原始业务消息。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'long-context.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        TrackingModel.summary_calls = TrackingModel.normal_calls = 0
        checkpoint = tmp_path / "checkpoint.db"
        inputs = ["第一轮：" + "甲" * 1000, "第二轮：" + "乙" * 1000]
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="long-context-owner", password="unused", role="user", status=1, is_deleted=0))
                db.add(
                    AgentConfig(
                        id=1,
                        owner_id=1,
                        name="Long context",
                        system_prompt="简洁回答",
                        default_model_id=1,
                        temperature=Decimal("1"),
                        max_output_tokens=256,
                        enabled=True,
                        version=1,
                        is_deleted=0,
                        status=1,
                    ),
                )
                db.add(
                    ProviderConnection(
                        id=1,
                        name="long-context-provider",
                        name_key="long-context-provider",
                        kind="ollama",
                        base_url="http://localhost:11434/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="small-context",
                        display_name="Small context",
                        verified_capabilities=["text"],
                        context_window=2048,
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                session = AgentSession(id="a" * 36, owner_id=1, agent_id=1, thread_id="b" * 36, title="长对话", archived=False, version=1)
                db.add(session)
                await db.flush()
            model = TrackingModel(responses=[AIMessage(content="fallback")])
            completed_runs = []
            for text in inputs:
                async with sessions.begin() as db:
                    run = await create_run(
                        db,
                        1,
                        agent_id=1,
                        model_id=1,
                        session_id=session.id,
                        thread_id=session.thread_id,
                        agent_config_version=1,
                        agent_system_prompt="简洁回答",
                        agent_temperature=Decimal("1.00"),
                        agent_max_output_tokens=256,
                        input_text=text,
                        max_model_calls=3,
                        max_total_tokens=500,
                        max_estimated_cost=Decimal("0.010000"),
                        cost_pricing=(Decimal("1"), Decimal("1"), "USD"),
                    )
                result = await execute_text_run(sessions, run.id, model, checkpoint)
                assert result.state == "completed", result.failure_code
                completed_runs.append(result)
            assert TrackingModel.summary_calls >= 1
            assert TrackingModel.normal_calls >= 2
            assert completed_runs[-1].model_call_count >= 2
            assert all(run.reported_total_tokens == run.model_call_count * 25 for run in completed_runs)
            assert all(run.estimated_cost == Decimal(run.model_call_count * 25) / 1_000_000 for run in completed_runs)
            async with sessions() as db:
                messages = (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))).all()
                assert [message.content for message in messages if message.role == "user"] == inputs
                assert [message.content for message in messages if message.role == "assistant"] == ["已收到", "已收到"]
            async with sessions.begin() as db:
                impossible = await create_run(
                    db,
                    1,
                    agent_id=1,
                    model_id=1,
                    session_id=session.id,
                    thread_id=session.thread_id,
                    agent_config_version=1,
                    agent_system_prompt="简洁回答",
                    agent_temperature=Decimal("1.00"),
                    agent_max_output_tokens=1800,
                    input_text="输出预算过高",
                )
            before = TrackingModel.normal_calls + TrackingModel.summary_calls
            rejected = await execute_text_run(sessions, impossible.id, model, checkpoint)
            assert rejected.state == "failed" and rejected.failure_code == "context_window_exceeded"
            assert TrackingModel.normal_calls + TrackingModel.summary_calls == before
            async with sessions.begin() as db:
                oversized = await create_run(
                    db,
                    1,
                    agent_id=1,
                    model_id=1,
                    session_id=session.id,
                    thread_id=session.thread_id,
                    agent_config_version=1,
                    agent_system_prompt="简洁回答",
                    agent_temperature=Decimal("1.00"),
                    agent_max_output_tokens=256,
                    input_text="超" * 10000,
                )
            rejected_input = await execute_text_run(sessions, oversized.id, model, checkpoint)
            assert rejected_input.state == "failed" and rejected_input.failure_code == "context_window_exceeded"
            assert TrackingModel.normal_calls + TrackingModel.summary_calls == before
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure_kind",
    ["error", "provider_timeout", "stream_timeout", "model_timeout", "connection_once", "connection_error", "connection_budget", "context_overflow"],
)
def test_summary_failure_keeps_visible_history_and_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str) -> None:
    """验证摘要失败与两层超时分别归类，并保留历史以供后续会话恢复。"""
    from langchain_openai import StreamChunkTimeoutError

    from openai import APIConnectionError, BadRequestError
    import httpx
    from app.core.config import get_config

    monkeypatch.setattr(get_config(), "AGENT_MODEL_CONNECTION_MAX_RETRIES", 1)
    monkeypatch.setattr(get_config(), "AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS", 0)
    original_generate = TrackingModel._agenerate
    failed_summary_attempts = 0

    async def generate_with_timeout(self, messages, stop=None, run_manager=None, **kwargs):
        """只在配置的摘要调用模拟超时，普通回复和后续恢复继续使用原模型。"""
        nonlocal failed_summary_attempts
        if failure_kind != "error" and type(self).fail_summary and any("压缩成准确的上下文摘要" in str(message.content) for message in messages):
            type(self).summary_calls += 1
            if failure_kind in {"connection_once", "connection_error", "connection_budget"}:
                failed_summary_attempts += 1
                if failure_kind == "connection_once" and failed_summary_attempts == 2:
                    return ChatResult(generations=[ChatGeneration(message=AIMessage(content="此前输入已压缩为摘要。"))])
                raise APIConnectionError(request=httpx.Request("POST", "https://private-summary-marker.test"))
            if failure_kind == "context_overflow":
                raise BadRequestError(
                    "private-context-summary-marker",
                    response=httpx.Response(400, request=httpx.Request("POST", "https://private-summary-marker.test")),
                    body={
                        "message": "This model's maximum context length is 65536 tokens. Please reduce the input tokens.",
                        "param": "input_tokens",
                    },
                )
            if failure_kind == "model_timeout":
                await asyncio.Event().wait()
            if failure_kind == "stream_timeout":
                raise StreamChunkTimeoutError(0.1, model_name="private-summary-model", chunks_received=1)
            raise TimeoutError("private-summary-read-timeout")
        return await original_generate(self, messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(TrackingModel, "_agenerate", generate_with_timeout)

    async def exercise() -> None:
        """在隔离环境执行异步场景：验证摘要失败返回明确终态，并保留历史以供后续会话恢复。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'summary-error.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "summary-error-checkpoint.db"
        TrackingModel.summary_calls = TrackingModel.normal_calls = 0
        TrackingModel.fail_summary = False
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="summary-error-owner", password="unused", role="user", status=1, is_deleted=0))
                db.add(
                    AgentConfig(
                        id=1,
                        owner_id=1,
                        name="Summary",
                        system_prompt="简洁回答",
                        default_model_id=1,
                        temperature=Decimal("1"),
                        max_output_tokens=256,
                        enabled=True,
                        version=1,
                        is_deleted=0,
                        status=1,
                    ),
                )
                db.add(
                    ProviderConnection(
                        id=1,
                        name="summary-error-provider",
                        name_key="summary-error-provider",
                        kind="ollama",
                        base_url="http://localhost:11434/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="small-context",
                        display_name="Small context",
                        verified_capabilities=["text"],
                        context_window=2048,
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                session = AgentSession(id="c" * 36, owner_id=1, agent_id=1, thread_id="d" * 36, title="摘要失败")
                db.add(session)
                await db.flush()
            model = TrackingModel(responses=[AIMessage(content="fallback")])
            for index in range(2):
                async with sessions.begin() as db:
                    run = await create_run(
                        db,
                        1,
                        agent_id=1,
                        model_id=1,
                        session_id=session.id,
                        thread_id=session.thread_id,
                        agent_config_version=1,
                        agent_system_prompt="简洁回答",
                        agent_temperature=Decimal("1"),
                        agent_max_output_tokens=256,
                        input_text=f"第{index + 1}轮：" + "甲" * 1000,
                        max_model_calls=1 if failure_kind == "connection_budget" and index == 1 else 3,
                        model_timeout_seconds=1 if failure_kind == "model_timeout" else 120,
                    )
                TrackingModel.fail_summary = index == 1
                result = await execute_text_run(sessions, run.id, model, checkpoint)
                expected_state = (
                    RunState.COMPLETED
                    if failure_kind == "connection_once"
                    else RunState.FAILED
                    if failure_kind in {"error", "connection_error", "connection_budget", "context_overflow"}
                    else RunState.TIMED_OUT
                )
                assert result.state == (RunState.COMPLETED if index == 0 else expected_state)
                if index == 1 and failure_kind == "connection_once":
                    assert failed_summary_attempts == 2 and result.model_call_count == 3
                if index == 1 and failure_kind != "connection_once":
                    expected_failure = (
                        RunModelFailureReason.CONTEXT
                        if failure_kind == "context_overflow"
                        else RunModelFailureReason.SUMMARY
                        if failure_kind in {"error", "connection_error"}
                        else "model_call_budget_exceeded"
                        if failure_kind == "connection_budget"
                        else RunTimeoutReason.MODEL
                        if failure_kind == "model_timeout"
                        else RunTimeoutReason.PROVIDER
                    )
                    assert result.failure_code == expected_failure
                    assert result.model_call_count >= 1
                    assert TrackingModel.summary_calls >= 1
                    async with sessions() as db:
                        messages = (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id))).all()
                        assert [message.role for message in messages] == ["user"]
            TrackingModel.fail_summary = False
            async with sessions.begin() as db:
                followup = await create_run(
                    db,
                    1,
                    agent_id=1,
                    model_id=1,
                    session_id=session.id,
                    thread_id=session.thread_id,
                    agent_config_version=1,
                    agent_system_prompt="简洁回答",
                    agent_temperature=Decimal("1"),
                    agent_max_output_tokens=256,
                    input_text="继续回答",
                    max_model_calls=3,
                )
            resumed = await execute_text_run(sessions, followup.id, model, checkpoint)
            assert resumed.state == "completed", resumed.failure_code
            async with sessions.begin() as db:
                later = await create_run(
                    db,
                    1,
                    agent_id=1,
                    model_id=1,
                    session_id=session.id,
                    thread_id=session.thread_id,
                    agent_config_version=1,
                    agent_system_prompt="简洁回答",
                    agent_temperature=Decimal("1"),
                    agent_max_output_tokens=256,
                    input_text="再继续一轮",
                    max_model_calls=3,
                )
            later_result = await execute_text_run(sessions, later.id, model, checkpoint)
            assert later_result.state == "completed", later_result.failure_code
        finally:
            TrackingModel.fail_summary = False
            await engine.dispose()

    asyncio.run(exercise())


def test_context_window_reserves_space_for_input_and_output() -> None:
    """验证上下文窗口为输入和输出预留空间且拒绝不足预算。"""
    config = RuntimeRunConfig(run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(), context_window=2048, max_output_tokens=256)
    assert config.summarization_limits() == (960, 128)
    too_small = RuntimeRunConfig(run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(), context_window=2048, max_output_tokens=1800)
    try:
        too_small.summarization_limits()
    except ValueError:
        pass
    else:
        raise AssertionError("上下文窗口应拒绝过高的输出预算")


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_OLLAMA_M4_LONG") != "1", reason="需显式启用真实长会话验收")
def test_live_ollama_long_session_summary(tmp_path: Path) -> None:
    """真实模型在小窗口内摘要旧消息，保留业务历史并继续回答。"""

    async def exercise() -> None:
        """在隔离业务库和检查点中连续执行两轮长输入。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'live-long.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "live-long-checkpoint.db"
        first_input = "请记住项目代号 ALPHA-42。下面是测试填充资料：" + "甲乙丙丁" * 250
        second_input = "请只回答之前的项目代号。下面是另一批测试填充资料：" + "戊己庚辛" * 250
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="m4-long", password="unused", role="user", status=1, is_deleted=0),
                        AgentConfig(
                            id=1,
                            owner_id=1,
                            name="Long context",
                            system_prompt="保留项目代号并简短回答。",
                            default_model_id=1,
                            temperature=Decimal("0"),
                            max_output_tokens=256,
                            enabled=True,
                            version=1,
                            is_deleted=0,
                            status=1,
                        ),
                        ProviderConnection(
                            id=1,
                            name="long-ollama",
                            name_key="long-ollama",
                            kind="ollama",
                            base_url="http://127.0.0.1:11434/v1",
                            timeout_seconds=120,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=1,
                            provider_id=1,
                            external_id="gemma4:31b-cloud",
                            display_name="Gemma4",
                            verified_capabilities=["text"],
                            context_window=2048,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        AgentSession(id="e" * 36, owner_id=1, agent_id=1, thread_id="f" * 36, title="真实长会话", archived=False, version=1),
                    ],
                )
            async with sessions() as db:
                model = build_chat_model(await db.get(ModelCatalog, 1), await db.get(ProviderConnection, 1))
            runs = []
            for text in (first_input, second_input):
                async with sessions.begin() as db:
                    run = await create_run(
                        db,
                        1,
                        agent_id=1,
                        model_id=1,
                        session_id="e" * 36,
                        thread_id="f" * 36,
                        agent_config_version=1,
                        agent_system_prompt="保留项目代号并简短回答。",
                        agent_temperature=Decimal("0"),
                        agent_max_output_tokens=256,
                        input_text=text,
                        max_model_calls=6,
                    )
                result = await execute_text_run(sessions, run.id, model, checkpoint, timeout_seconds=120)
                assert result.state == "completed", result.failure_code
                runs.append(result)
            assert runs[-1].model_call_count >= 2
            async with sessions() as db:
                messages = (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))).all()
                assert [item.content for item in messages if item.role == "user"] == [first_input, second_input]
                assert "ALPHA-42" in [item.content for item in messages if item.role == "assistant"][-1]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_OLLAMA_M4_LONG") != "1", reason="需显式启用真实长会话验收")
def test_live_ollama_tool_history_survives_summary(tmp_path: Path) -> None:
    """真实模型读取采购简报后，摘要历史仍能回答项目编号。"""

    async def exercise() -> None:
        """用隔离业务库和检查点验证工具结果跨长会话摘要。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'live-tool-summary.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "live-tool-summary-checkpoint.db"
        tool_reads = 0

        @tool
        def read_project_brief() -> str:
            """读取星河项目采购简报中的已确认事实。"""
            nonlocal tool_reads
            tool_reads += 1
            return "采购项目 XINGHE-27，预算 CNY 180000，交付日期 2026-12-15，负责人 Lin Qing。"

        async def resolve_tools(db, run):
            """首轮开放简报读取，后续轮次只使用已保存的历史上下文。"""
            return ([read_project_brief], {}) if tool_reads == 0 else ([], {})

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="m4-live-tool-summary", password="unused", role="user", status=1, is_deleted=0),
                        AgentConfig(
                            id=1,
                            owner_id=1,
                            name="采购助手",
                            system_prompt="需要项目事实时先调用可用工具；回答必须保留原始项目编号。",
                            default_model_id=1,
                            temperature=Decimal("0"),
                            max_output_tokens=256,
                            enabled=True,
                            version=1,
                            is_deleted=0,
                            status=1,
                        ),
                        ProviderConnection(
                            id=1,
                            name="long-tool-ollama",
                            name_key="long-tool-ollama",
                            kind="ollama",
                            base_url="http://127.0.0.1:11434/v1",
                            timeout_seconds=120,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=1,
                            provider_id=1,
                            external_id="gemma4:31b-cloud",
                            display_name="Gemma4",
                            verified_capabilities=["text", "tools"],
                            context_window=2048,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        AgentSession(id="c" * 36, owner_id=1, agent_id=1, thread_id="d" * 36, title="采购长会话"),
                    ],
                )
            async with sessions() as db:
                model = build_chat_model(await db.get(ModelCatalog, 1), await db.get(ProviderConnection, 1))

            async def run_turn(text: str):
                """提交并执行一轮采购会话，返回持久化运行结果。"""
                async with sessions.begin() as db:
                    run = await create_run(
                        db,
                        1,
                        agent_id=1,
                        model_id=1,
                        session_id="c" * 36,
                        thread_id="d" * 36,
                        agent_config_version=1,
                        agent_system_prompt="需要项目事实时先调用可用工具；回答必须保留原始项目编号。",
                        agent_temperature=Decimal("0"),
                        agent_max_output_tokens=256,
                        input_text=text,
                        max_model_calls=6,
                    )
                result = await execute_text_run(sessions, run.id, model, checkpoint, resolve_tools=resolve_tools, timeout_seconds=120)
                assert result.state == "completed", (result.failure_code, result.model_call_count, result.tool_call_count)
                return result

            first_input = "先读取 read_project_brief 工具中的采购简报，只回答项目编号。"
            first = await run_turn(first_input)
            assert first.tool_call_count == 1 and tool_reads == 1
            second_input = "不再调用工具，请仅回答先前简报中的项目编号。下面是无关测试资料：" + "甲乙丙丁" * 250
            second = await run_turn(second_input)
            assert second.model_call_count >= 2 and tool_reads == 1
            async with sessions() as db:
                messages = (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))).all()
                assert [message.content for message in messages if message.role == "user"] == [first_input, second_input]
                assert "XINGHE-27" in [message.content for message in messages if message.role == "assistant"][-1]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_summarized_source_can_be_read_from_current_thread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """摘要只保留概述后，模型仍可按范围读取当前线程中的原始附件，避免生成未获取正文的空报告。"""
    import re
    from langchain_core.messages import ToolMessage

    monkeypatch.setattr(RuntimeRunConfig, "summarization_limits", lambda self: (800, 80))
    observed = []

    class SourceRecoveryModel(TrackingModel):
        """使用摘要提供的实际路径回读原文，强制核对工具是否可见且成功。"""

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            """记录摘要后的模型实际可用工具，禁止测试绕过工具披露。"""
            observed.append([item.name for item in tools])
            return self

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            """摘要故意省略关键事实，业务回答必须通过历史读取恢复事实。"""
            if any("压缩成准确的上下文摘要" in str(message.content) for message in messages):
                recovered_fact = any(
                    isinstance(message, ToolMessage) and message.name == "read_file" and "设计锚固长度为12米" in str(message.content) for message in messages
                )
                answer = AIMessage(content="通过原文回读确认：设计锚固长度为12米" if recovered_fact else "附件已转存，请按需回读历史原文。")
            elif any(
                isinstance(message, ToolMessage)
                and "设计锚固长度为12米" in str(message.content)
                or "通过原文回读确认：设计锚固长度为12米" in str(message.content)
                for message in messages
            ):
                answer = AIMessage(content="设计锚固长度为12米")
            elif any(isinstance(message, ToolMessage) and message.name == "read_file" for message in messages):
                answer = AIMessage(content="未获取正文")
            else:
                content = "\n".join(str(message.content) for message in messages)
                match = re.search(r"/conversation_history/[^\s]+\.md", content)
                if match is None:
                    return ChatResult(
                        generations=[
                            ChatGeneration(
                                message=AIMessage(
                                    content="",
                                    tool_calls=[
                                        {
                                            "name": "prepare_review",
                                            "args": {},
                                            "id": "prepare-review",
                                        },
                                    ],
                                ),
                            ),
                        ],
                    )
                assert "read_file" in observed[-1], "摘要后的模型必须能发现原文读取工具"
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": match.group(),
                                "offset": 0,
                                "limit": 12,
                            },
                            "id": "recover-source",
                        },
                    ],
                )
            return ChatResult(generations=[ChatGeneration(message=answer)])

    async def exercise() -> None:
        """通过真实摘要、虚拟文件存储、工具授权和执行器验证原文恢复。"""

        @tool
        def prepare_review() -> str:
            """在摘要前形成完整的工具轮次，模拟读取 Skill 后开始审查。"""
            return "审查已准备。"

        async def resolve_tools(db, run):
            """只提供用于准备审查的只读工具，历史读取由平台控制。"""
            return [prepare_review], {}

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recover-source.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="source-recovery-owner", password="unused", role="user"))
                run = await create_run(db, 1, input_text="设计锚固长度为12米\n" + "一般施工说明。\n" * 600, max_model_calls=5)
            result = await execute_text_run(
                sessions,
                run.id,
                SourceRecoveryModel(responses=[AIMessage(content="unused")]),
                tmp_path / "recover-source-checkpoint.db",
                resolve_tools=resolve_tools,
            )
            assert result.state == "completed", result.failure_code
            assert result.tool_call_count == 2
            async with sessions() as db:
                answer = await db.scalar(select(AgentRunMessage.content).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == "assistant"))
                assert answer == "设计锚固长度为12米"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("window", [65536, None])
def test_catalog_window_reaches_runtime_model_profile(window: int | None) -> None:
    """目录中的实际部署窗口必须交给运行时检查完整请求，未设置时保留原有能力信息。"""
    from app.services.runtime_adapter import DeepAgentsRuntimeAdapter

    model = TrackingModel(responses=[AIMessage(content="unused")])
    model.profile = {
        "max_input_tokens": 131072,
        "tool_calling": True,
    }
    config = RuntimeRunConfig(
        run_id="profile-run",
        owner_id=1,
        thread_id="profile-thread",
        model_id=4,
        tool_names=(),
        context_window=window,
        max_output_tokens=16384,
    )
    DeepAgentsRuntimeAdapter(config, model=model, tools=[], interrupt_on={}, checkpointer=None, middleware=[])
    assert model.profile["max_input_tokens"] == (window if window is not None else 131072)
    assert model.profile["tool_calling"] is True


def test_runtime_read_schema_replaces_warm_media_schema() -> None:
    """即使默认文件工具已缓存多媒体契约，模型仍只看到已授权的工具和三个文本分页字段。"""
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.messages import HumanMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.services.runtime_adapter import RuntimeSummarizationMiddleware, count_context_tokens

    async def exercise() -> None:
        """经过真实摘要包装层，检查请求工具契约及原工具对象均符合预期。"""
        filesystem_tools = FilesystemMiddleware(backend=StateBackend()).tools
        original = next(item for item in filesystem_tools if item.name == "read_file")
        original_schema = convert_to_openai_tool(original)
        assert len(original_schema["function"]["description"]) > 1000
        model = TrackingModel(
            responses=[AIMessage(content="unused")],
            profile={
                "max_input_tokens": 65536,
            },
        )
        middleware = RuntimeSummarizationMiddleware(
            allowed_names=frozenset(),
            model=model,
            backend=StateBackend(),
            trigger=("tokens", 30000),
            keep=("tokens", 1000),
            token_counter=count_context_tokens,
        )
        calls = []

        async def handler(request):
            """记录经过过滤的请求，防止默认工具或缓存的媒体契约重新进入预算。"""
            calls.append(request)
            assert len(request.tools) == 1
            assert request.tools[0] is not original
            schema = convert_to_openai_tool(request.tools[0])
            properties = schema["function"]["parameters"]["properties"]
            assert set(properties) == {"file_path", "offset", "limit"}
            assert properties["file_path"]["description"] == "已授权的 Skill 文件或当前会话历史文件路径"
            assert len(schema["function"]["description"]) < 200
            return ModelResponse(result=[AIMessage(content="完成")])

        await middleware.awrap_model_call(
            ModelRequest(model=model, messages=[HumanMessage(content="查看文本")], tools=filesystem_tools, state={}),
            handler,
        )
        assert len(calls) == 1
        assert convert_to_openai_tool(original) == original_schema

    asyncio.run(exercise())


def test_complete_request_budget_reserves_output_and_counts_system_and_tools() -> None:
    """正文单独可以装入窗口，系统指令与工具契约加入并预留输出后必须在发请求前拒绝。"""
    from deepagents.backends import StateBackend
    from deepagents.middleware.summarization import ContextOverflowError
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.messages import HumanMessage, SystemMessage
    from unittest.mock import AsyncMock

    from app.services.runtime_adapter import RuntimeSummarizationMiddleware, count_context_tokens

    async def exercise() -> None:
        """以不可压缩的单条消息和完整请求开销检验真正生效的预算保护。"""

        @tool
        def review_plan(text: str) -> str:
            """校验施工方案。"""
            return text

        model = TrackingModel(
            responses=[AIMessage(content="unused")],
            profile={
                "max_input_tokens": 4096,
            },
        )
        middleware = RuntimeSummarizationMiddleware(
            allowed_names=frozenset({"review_plan"}),
            model=model,
            backend=StateBackend(),
            trigger=("tokens", 100000),
            keep=("tokens", 128),
            token_counter=count_context_tokens,
        )
        messages = [HumanMessage(content="施工参数。" * 100)]
        request = ModelRequest(
            model=model,
            messages=messages,
            system_message=SystemMessage(content="审查规则。" * 150),
            tools=[review_plan],
            model_settings={
                "max_completion_tokens": 3072,
            },
            state={},
        )
        budget = int(4096 * 0.95) - 3072
        assert count_context_tokens(messages) < budget
        assert count_context_tokens(messages + [request.system_message], tools=[review_plan]) > budget
        handler = AsyncMock()
        with pytest.raises(ContextOverflowError):
            await middleware.awrap_model_call(request, handler)
        handler.assert_not_called()

    asyncio.run(exercise())
