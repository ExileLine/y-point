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
                    )
                )
            ]
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
                    tool_calls=[{"name": "read_project_brief", "args": {}, "id": "read-project-brief"}],
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
                    ]
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
                second_input = "请只回答采购项目编号。以下是本轮填充资料：" + "甲乙丙丁" * 500
                async with sessions.begin() as db:
                    second = await create_run(db, 1, input_text=second_input, **snapshot)
                resumed = await execute_text_run(sessions, second.id, model, checkpoint, resolve_tools=resolve_tools)
                assert resumed.state == "completed", resumed.failure_code
                assert ToolHistoryModel.summary_calls >= 1
                assert any("星河-27" in prompt for prompt in ToolHistoryModel.summary_prompts)
                async with sessions() as db:
                    messages = (
                        await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))
                    ).all()
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
        inputs = ["第一轮：" + "甲" * 2600, "第二轮：" + "乙" * 2600]
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
                    )
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
                    )
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
                    )
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
                messages = (
                    await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))
                ).all()
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


def test_summary_failure_keeps_visible_history_and_reports_failure(tmp_path: Path) -> None:
    """验证摘要失败返回明确终态，并保留历史以供后续会话恢复。"""

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
                    )
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
                    )
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
                    )
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
                        input_text=f"第{index + 1}轮：" + "甲" * 2600,
                        max_model_calls=3,
                    )
                TrackingModel.fail_summary = index == 1
                result = await execute_text_run(sessions, run.id, model, checkpoint)
                assert result.state == ("completed" if index == 0 else "failed")
                if index == 1:
                    assert result.failure_code == "summary_error"
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
    config = RuntimeRunConfig(
        run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(), context_window=2048, max_output_tokens=256
    )
    assert config.summarization_limits() == (960, 128)
    too_small = RuntimeRunConfig(
        run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(), context_window=2048, max_output_tokens=1800
    )
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
        first_input = "请记住项目代号 ALPHA-42。下面是测试填充资料：" + "甲乙丙丁" * 500
        second_input = "请只回答之前的项目代号。下面是另一批测试填充资料：" + "戊己庚辛" * 500
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
                        AgentSession(
                            id="e" * 36, owner_id=1, agent_id=1, thread_id="f" * 36, title="真实长会话", archived=False, version=1
                        ),
                    ]
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
                messages = (
                    await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))
                ).all()
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
                    ]
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
            second_input = "不再调用工具，请仅回答先前简报中的项目编号。下面是无关测试资料：" + "甲乙丙丁" * 500
            second = await run_turn(second_input)
            assert second.model_call_count >= 2 and tool_reads == 1
            async with sessions() as db:
                messages = (
                    await db.scalars(select(AgentRunMessage).where(AgentRunMessage.owner_id == 1).order_by(AgentRunMessage.id))
                ).all()
                assert [message.content for message in messages if message.role == "user"] == [first_input, second_input]
                assert "XINGHE-27" in [message.content for message in messages if message.role == "assistant"][-1]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
