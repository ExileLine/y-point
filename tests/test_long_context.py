"""验证小窗口模型的长会话摘要边界。"""

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run
from app.services.runtime_adapter import RuntimeRunConfig


class TrackingModel(FakeMessagesListChatModel):
    summary_calls: ClassVar[int] = 0
    normal_calls: ClassVar[int] = 0

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if any("压缩成准确的上下文摘要" in str(message.content) for message in messages):
            type(self).summary_calls += 1
            answer = "此前的用户输入已压缩为摘要。"
        else:
            type(self).normal_calls += 1
            answer = "已收到"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=answer, usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
        ))])


def test_long_session_summarizes_without_changing_visible_messages(tmp_path: Path) -> None:
    async def exercise() -> None:
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
                db.add(ProviderConnection(
                    id=1, name="long-context-provider", name_key="long-context-provider", kind="ollama",
                    base_url="http://localhost:11434/v1", creator_id=1, status=1, is_deleted=0,
                ))
                db.add(ModelCatalog(
                    id=1, provider_id=1, external_id="small-context", display_name="Small context",
                    verified_capabilities=["text"], context_window=2048,
                    creator_id=1, status=1, is_deleted=0,
                ))
                session = AgentSession(
                    id="a" * 36, owner_id=1, agent_id=1, thread_id="b" * 36,
                    title="长对话", archived=False, version=1,
                )
                db.add(session)
                await db.flush()
            model = TrackingModel(responses=[AIMessage(content="fallback")])
            completed_runs = []
            for text in inputs:
                async with sessions.begin() as db:
                    run = await create_run(
                        db, 1, agent_id=1, model_id=1, session_id=session.id, thread_id=session.thread_id,
                        agent_config_version=1, agent_system_prompt="简洁回答", agent_temperature=Decimal("1.00"),
                        agent_max_output_tokens=256, input_text=text, max_model_calls=3,
                        max_total_tokens=500, max_estimated_cost=Decimal("0.010000"),
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
                messages = (await db.scalars(select(AgentRunMessage).where(
                    AgentRunMessage.owner_id == 1,
                ).order_by(AgentRunMessage.id))).all()
                assert [message.content for message in messages if message.role == "user"] == inputs
                assert [message.content for message in messages if message.role == "assistant"] == ["已收到", "已收到"]
            async with sessions.begin() as db:
                impossible = await create_run(
                    db, 1, agent_id=1, model_id=1, session_id=session.id, thread_id=session.thread_id,
                    agent_config_version=1, agent_system_prompt="简洁回答", agent_temperature=Decimal("1.00"),
                    agent_max_output_tokens=1800, input_text="输出预算过高",
                )
            before = TrackingModel.normal_calls + TrackingModel.summary_calls
            rejected = await execute_text_run(sessions, impossible.id, model, checkpoint)
            assert rejected.state == "failed" and rejected.failure_code == "context_window_exceeded"
            assert TrackingModel.normal_calls + TrackingModel.summary_calls == before
            async with sessions.begin() as db:
                oversized = await create_run(
                    db, 1, agent_id=1, model_id=1, session_id=session.id, thread_id=session.thread_id,
                    agent_config_version=1, agent_system_prompt="简洁回答", agent_temperature=Decimal("1.00"),
                    agent_max_output_tokens=256, input_text="超" * 10000,
                )
            rejected_input = await execute_text_run(sessions, oversized.id, model, checkpoint)
            assert rejected_input.state == "failed" and rejected_input.failure_code == "context_window_exceeded"
            assert TrackingModel.normal_calls + TrackingModel.summary_calls == before
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_context_window_reserves_space_for_input_and_output() -> None:
    config = RuntimeRunConfig(
        run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(),
        context_window=2048, max_output_tokens=256,
    )
    assert config.summarization_limits() == (960, 128)
    too_small = RuntimeRunConfig(
        run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(),
        context_window=2048, max_output_tokens=1800,
    )
    try:
        too_small.summarization_limits()
    except ValueError:
        pass
    else:
        raise AssertionError("上下文窗口应拒绝过高的输出预算")
