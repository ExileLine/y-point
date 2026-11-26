# -*- coding: utf-8 -*-
# @Time    : 2026-11-26 11:57:36
# @Author  : yangyuexiong
# @File    : test_cancelled_context.py

"""验证主动取消后的公开正文衔接、工具剥离和历史容量与授权边界。"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import MessageRole, RunEventType, RunState, UserRole
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.base import Base
from app.services import run_recovery as recovery_module
from app.services.run_executor import execute_text_run
from app.services.run_manager import (
    append_model_boundary,
    append_reasoning_delta,
    append_text_delta,
    claim_run,
    create_run,
    get_owned_messages,
    replay_events,
    request_cancel,
    acknowledge_cancel,
)
from app.services.run_recovery import CANCELLED_CONTEXT_NOTE, cancelled_messages_since, cancelled_run_messages
from tests.test_run_executor import ScriptedModel


@asynccontextmanager
async def cancelled_environment(tmp_path: Path):
    """创建独立业务库及两个用户，所有模型和工具均由测试控制。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancelled-context.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add_all([Admin(id=owner, username=f"cancelled-{owner}", password="unused", role=UserRole.USER) for owner in (1, 2)])
        yield sessions
    finally:
        await engine.dispose()


@pytest.mark.parametrize("published", [True, False])
def test_cancelled_stream_inherits_only_published_answer(tmp_path: Path, published: bool) -> None:
    """真实流式取消只继承已公开正文，预算保护下的正文和独立思考不得带入下一轮。"""

    async def exercise() -> None:
        """在正文事件已提交后取消，再观察下一次模型请求。"""
        started = asyncio.Event()
        captured: list[BaseMessage] = []
        partial = "甲方案成本较低，但建设周期较长。乙方案"
        reasoning = "独立思考内容不能作为历史回答"

        class StreamingModel(ScriptedModel):
            """发出半段正文及独立思考后阻塞，等待用户取消。"""

            async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
                """真实生成消息块，随后由执行器中断模型协程。"""
                yield ChatGenerationChunk(
                    message=AIMessageChunk(
                        content=partial,
                        additional_kwargs={
                            "reasoning_content": reasoning,
                        },
                    ),
                )
                started.set()
                await asyncio.Event().wait()

        class FollowupModel(ScriptedModel):
            """捕获衔接请求，避免使用模型回答内容猜测上下文是否正确。"""

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                """记录发给模型的实际消息。"""
                captured.extend(messages)
                return await super()._agenerate(messages, stop, run_manager, **kwargs)

        async with cancelled_environment(tmp_path) as sessions:
            checkpoint = tmp_path / "stream-checkpoint.db"
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="请比较甲乙两种方案", max_total_tokens=None if published else 10000)
            task = asyncio.create_task(
                execute_text_run(sessions, run.id, StreamingModel(responses=[]), checkpoint, lease_seconds=2, poll_seconds=0.05),
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            async with asyncio.timeout(5):
                while True:
                    async with sessions() as db:
                        events = await replay_events(db, run.id, 1)
                    if any(event.event_type == RunEventType.REASONING_DELTA for event in events):
                        break
                    await asyncio.sleep(0.01)
            async with sessions.begin() as db:
                await request_cancel(db, run.id, 1)
            result = await asyncio.wait_for(task, timeout=5)
            assert result.state == RunState.CANCELLED
            async with sessions() as db:
                assert [message.role for message in await get_owned_messages(db, run.id, 1)] == [MessageRole.USER]
                events = await replay_events(db, run.id, 1)
                assert any(event.event_type == RunEventType.TEXT_DELTA for event in events) is published
            async with sessions.begin() as db:
                followup = await create_run(db, 1, thread_id=run.thread_id, input_text="继续")
            resumed = await execute_text_run(sessions, followup.id, FollowupModel(responses=[AIMessage(content="接着比较乙方案")]), checkpoint)
            assert resumed.state == RunState.COMPLETED, resumed.failure_code
            context = "\n".join(str(message.content) for message in captured)
            assert "请比较甲乙两种方案" in context
            assert (partial in context) is published
            assert reasoning not in context
            assert CANCELLED_CONTEXT_NOTE in context
            assert all(not isinstance(message, ToolMessage) and not getattr(message, "tool_calls", []) for message in captured)

    asyncio.run(exercise())


@pytest.mark.parametrize("safe_read", [True, False])
def test_cancelled_tool_drops_unclosed_calls_without_replaying(tmp_path: Path, safe_read: bool) -> None:
    """可安全放弃的读取只继承正文，未知外部操作继续受检查点安全限制，不重放未闭合调用。"""

    async def exercise() -> None:
        """在工具执行中取消，验证下一轮上下文和实际工具执行次数。"""
        started = asyncio.Event()
        executions = []
        captured: list[BaseMessage] = []
        tool_name = "list_session_artifacts" if safe_read else "external_write"

        @tool(tool_name)
        async def interrupted_tool() -> str:
            """模拟读取或外部操作中的阻塞，取消不能自动重做。"""
            executions.append(tool_name)
            started.set()
            await asyncio.Event().wait()
            return "未返回的私有工具结果"

        async def resolve_tools(db, run):
            """为两轮请求提供同一工具，借执行计数发现隐式重放。"""
            return [interrupted_tool], {}

        class FollowupModel(ScriptedModel):
            """观察新轮是否包含任何旧工具协议消息。"""

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                """捕获上下文并返回纯文本回答。"""
                captured.extend(messages)
                return await super()._agenerate(messages, stop, run_manager, **kwargs)

        async with cancelled_environment(tmp_path) as sessions:
            checkpoint = tmp_path / "tool-checkpoint.db"
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="请整理两种方案的比较")
            task = asyncio.create_task(
                execute_text_run(
                    sessions,
                    run.id,
                    ScriptedModel(
                        responses=[
                            AIMessage(
                                content="我先整理已有资料，再比较成本。",
                                tool_calls=[
                                    {
                                        "name": tool_name,
                                        "args": {},
                                        "id": "unclosed-tool-call",
                                    },
                                ],
                            ),
                        ],
                    ),
                    checkpoint,
                    resolve_tools=resolve_tools,
                    lease_seconds=2,
                    poll_seconds=0.05,
                ),
            )
            await asyncio.wait_for(started.wait(), timeout=5)
            async with sessions.begin() as db:
                await request_cancel(db, run.id, 1)
            assert (await asyncio.wait_for(task, timeout=5)).state == RunState.CANCELLED
            async with sessions.begin() as db:
                followup = await create_run(db, 1, thread_id=run.thread_id, input_text="继续")
            resumed = await execute_text_run(
                sessions,
                followup.id,
                FollowupModel(responses=[AIMessage(content="接着比较成本")]),
                checkpoint,
                resolve_tools=resolve_tools,
            )
            assert executions == [tool_name]
            if safe_read:
                assert resumed.state == RunState.COMPLETED, resumed.failure_code
                assert any(message.content == "请整理两种方案的比较" for message in captured)
                assert any("我先整理已有资料，再比较成本。" in str(message.content) for message in captured)
                assert all(not isinstance(message, ToolMessage) and not getattr(message, "tool_calls", []) for message in captured)
            else:
                assert resumed.state == RunState.FAILED and resumed.failure_code == "checkpoint_mismatch"
                assert not captured

    asyncio.run(exercise())


def test_cancelled_history_is_scoped_ordered_and_bounded(tmp_path: Path) -> None:
    """只重建同用户同会话同线程取消轮，公开正文按顺序拼接，超长内容明确截断。"""

    async def exercise() -> None:
        """用持久化用户消息及事件核对正文来源、权限过滤、容量和旧状态不变。"""
        async with cancelled_environment(tmp_path) as sessions:
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="保留的原问题")
                run = await claim_run(db, run.id)
                await append_model_boundary(db, run.id, run.lease_token, "first")
                await append_text_delta(db, run.id, run.lease_token, "第一段。")
                await append_reasoning_delta(db, run.id, run.lease_token, "不能带入的思考")
                await append_model_boundary(db, run.id, run.lease_token, "second")
                await append_text_delta(db, run.id, run.lease_token, "第二段。" + "长正文" * 200)
                await request_cancel(db, run.id, 1)
                run = await acknowledge_cancel(db, run.id, run.lease_token)
                for owner, session_id, agent_id, thread_id in [
                    (2, None, None, run.thread_id),
                    (1, "另一会话", None, run.thread_id),
                    (1, None, 91, run.thread_id),
                    (1, None, None, "另一线程"),
                ]:
                    other = AgentRun(
                        id=str(uuid4()),
                        owner_id=owner,
                        session_id=session_id,
                        agent_id=agent_id,
                        thread_id=thread_id,
                        state=RunState.CANCELLED,
                    )
                    db.add(other)
                    db.add(AgentRunMessage(run_id=other.id, owner_id=owner, role=MessageRole.USER, content="不能跨范围读取的问题"))
                    await db.flush()
                followup = AgentRun(id=str(uuid4()), owner_id=1, thread_id=run.thread_id, state=RunState.QUEUED)
                db.add(followup)
                db.add(AgentRunMessage(run_id=followup.id, owner_id=1, role=MessageRole.USER, content="继续"))
                await db.flush()
                message = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id))
                bounded = await cancelled_run_messages(db, run, message, max_characters=300)
                assert sum(len(item.content) for item in bounded) <= 300
                assert bounded[0].content == "保留的原问题"
                assert "第一段。\n\n第二段。" in bounded[1].content
                assert "截断" in bounded[1].content and CANCELLED_CONTEXT_NOTE in bounded[1].content
                recovered = await cancelled_messages_since(db, followup, None)
                assert [item.content for item in recovered if item.type == "human"] == ["保留的原问题"]
                assert "不能带入的思考" not in str(recovered)
                assert (await db.get(AgentRun, run.id)).state == RunState.CANCELLED
                assert [item.role for item in await get_owned_messages(db, run.id, 1)] == [MessageRole.USER]
                run.state = RunState.FAILED
                assert await cancelled_run_messages(db, run, message) == []
                run.state = RunState.CANCELLED
                message.owner_id = 2
                assert await cancelled_run_messages(db, run, message) == []

    asyncio.run(exercise())


@pytest.mark.parametrize("previous_success", [False, True])
def test_queued_cancel_keeps_question_without_checkpoint(tmp_path: Path, previous_success: bool) -> None:
    """尚未进入模型即取消的用户问题也能成为下一轮上下文。"""

    async def exercise() -> None:
        """验证排队取消不依赖取消轮检查点或已生成文件。"""
        captured: list[BaseMessage] = []

        class FollowupModel(ScriptedModel):
            """捕获取消轮没有检查点时重建的上下文。"""

            async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
                """返回正常回答并保存请求消息。"""
                captured.extend(messages)
                return await super()._agenerate(messages, stop, run_manager, **kwargs)

        async with cancelled_environment(tmp_path) as sessions:
            checkpoint = tmp_path / "queued-checkpoint.db"
            async with sessions.begin() as db:
                first = await create_run(db, 1, input_text="已完成的问题" if previous_success else "排队时提出的问题")
            if previous_success:
                await execute_text_run(sessions, first.id, ScriptedModel(responses=[AIMessage(content="已完成的回答")]), checkpoint)
                async with sessions.begin() as db:
                    cancelled = await create_run(db, 1, thread_id=first.thread_id, input_text="排队时提出的问题")
            else:
                cancelled = first
            async with sessions.begin() as db:
                assert (await request_cancel(db, cancelled.id, 1)).state == RunState.CANCELLED
                followup = await create_run(db, 1, thread_id=first.thread_id, input_text="继续")
            result = await execute_text_run(sessions, followup.id, FollowupModel(responses=[AIMessage(content="继续处理原问题")]), checkpoint)
            assert result.state == RunState.COMPLETED, result.failure_code
            assert any(item.content == "排队时提出的问题" for item in captured)
            assert any(CANCELLED_CONTEXT_NOTE in str(item.content) for item in captured)

    asyncio.run(exercise())


@pytest.mark.parametrize("turn_limit", [1, 100])
def test_cancelled_history_limits_keep_recent_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turn_limit: int) -> None:
    """轮数或字符上限触发时仍保留最近原问题与中断标记，省略说明也计入总容量。"""

    async def exercise() -> None:
        """通过真实消息与公开事件构造多次取消，核对完整输出预算。"""
        monkeypatch.setattr(recovery_module, "MAX_CANCELLED_CONTEXT_TURNS", turn_limit)
        monkeypatch.setattr(recovery_module, "MAX_CANCELLED_CONTEXT_CHARACTERS", 300)
        async with cancelled_environment(tmp_path) as sessions:
            thread_id = None
            async with sessions.begin() as db:
                for question in ("较早的原问题", "最近的原问题"):
                    cancelled = await create_run(db, 1, thread_id=thread_id, input_text=question)
                    thread_id = cancelled.thread_id
                    cancelled = await claim_run(db, cancelled.id)
                    await append_text_delta(db, cancelled.id, cancelled.lease_token, "很长的公开正文。" * 100)
                    await request_cancel(db, cancelled.id, 1)
                    await acknowledge_cancel(db, cancelled.id, cancelled.lease_token)
                followup = await create_run(db, 1, thread_id=thread_id, input_text="继续")
                messages = await cancelled_messages_since(db, followup, None)
                text = "\n".join(str(message.content) for message in messages)
                assert sum(len(message.content) for message in messages) <= 300
                assert "最近的原问题" in text and "较早的原问题" not in text
                assert "未携带" in text and "截断" in text and CANCELLED_CONTEXT_NOTE in text

    asyncio.run(exercise())
