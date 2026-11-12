# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 21:53:35
# @Author  : yangyuexiong
# @File    : test_chat_presentation.py

"""验证真实 SDK 思考流、持久化回放、调用耗时与历史展示权限。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessageChunk
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import ModelThinkingMode, RunModelFailureReason, RunCallKind, RunEventType, RunState, ToolKind, Visibility
from app.models.admin import Admin
from app.models.agent_run_event import AgentRunEvent
from app.models.base import Base, shanghai_now
from app.models.tool import RunTool, UserTool
from app.services.model_stream import ReasoningChatOpenAI, public_reasoning_text
from app.services.run_calls import load_run_calls
from app.services.run_executor import _stream_model_text
from app.services.run_manager import RunConflictError, append_reasoning_delta, append_text_delta, claim_run, complete_run, create_run, replay_events
from app.services.run_presentation import load_run_presentation


@pytest.mark.parametrize("enabled", [True, False])
def test_sdk_reasoning_stream_replay_and_metrics(tmp_path: Path, enabled: bool) -> None:
    """真实 SDK 解析兼容 SSE，正文和思考分流，停用时不保存思考，终态可恢复统计。"""

    async def exercise() -> None:
        """用隔离数据库和真实 SDK 的内存 HTTP 流完成授权写入及回放。"""
        packets = [
            {
                "reasoning_content": "先核对资料。" * 500,
                "signature": "不得泄露的签名",
                "encrypted_content": "不得泄露的密文",
            },
            {
                "reasoning_content": "再形成结论。",
            },
            {
                "content": "最终回答",
            },
        ]
        body = (
            "".join(
                "data: "
                + json.dumps(
                    {
                        "id": "chat-fixture",
                        "object": "chat.completion.chunk",
                        "model": "fixture",
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": None,
                            },
                        ],
                    },
                )
                + "\n\n"
                for delta in packets
            )
            + "data: [DONE]\n\n"
        )

        def respond(request: httpx.Request) -> httpx.Response:
            """仅在内存中返回兼容模型数据，不调用真实供应商。"""
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                },
                content=body,
            )

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'presentation.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="reader", password="not-used"))
                run = await create_run(db, 1, input_text="请核对")
                run.agent_thinking_mode = ModelThinkingMode.AUTO if enabled else ModelThinkingMode.DISABLED
                run.reported_total_tokens = 1234
                await db.flush()
                run = await claim_run(db, run.id)
                token = run.lease_token
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                model = ReasoningChatOpenAI(model="fixture", api_key="fixture", base_url="https://fixture.invalid/v1", http_async_client=client)

                class Runtime:
                    """只替换图封装，使用真实 SDK 消费网络协议。"""

                    async def events(self, invocation):
                        """模型节点向运行执行器提供实际 SDK 消息。"""
                        async for chunk in model.astream("请核对"):
                            yield (
                                "messages",
                                (
                                    chunk,
                                    {
                                        "langgraph_node": "model",
                                    },
                                ),
                            )
                        yield (
                            "messages",
                            (
                                AIMessageChunk(
                                    content="工具节点秘密",
                                    additional_kwargs={
                                        "reasoning_content": "不公开的其他节点",
                                    },
                                ),
                                {
                                    "langgraph_node": "tools",
                                },
                            ),
                        )

                await _stream_model_text(Runtime(), {}, sessions, run.id, token, publish_reasoning=enabled)
            async with sessions.begin() as db:
                await complete_run(db, run.id, token, checkpoint_id="checkpoint-fixture", final_text="最终回答")
            async with sessions() as db:
                events = await replay_events(db, run.id, 1)
                assert [event.sequence for event in events] == list(range(1, len(events) + 1))
                assert all(len(event.content or "") <= 2048 for event in events)
                assert "".join(event.content or "" for event in events if event.event_type == RunEventType.TEXT_DELTA) == "最终回答"
                expected = packets[0]["reasoning_content"] + packets[1]["reasoning_content"] if enabled else ""
                assert "".join(event.content or "" for event in events if event.event_type == RunEventType.REASONING_DELTA) == expected
                public = await load_run_presentation(db, 1, [run.id])
                assert public[run.id]["reasoning_content"] == expected
                assert public[run.id]["metrics"]["total_tokens"] == 1234
                assert public[run.id]["metrics"]["elapsed_ms"] >= 0
                assert await load_run_presentation(db, 2, [run.id]) == {}
                assert not any("秘密" in (event.content or "") or "不得泄露" in (event.content or "") for event in events)
                tail = await replay_events(db, run.id, 1, after=events[-2].sequence)
                assert len(tail) == 1
            with pytest.raises(RunConflictError):
                async with sessions.begin() as db:
                    await append_reasoning_delta(db, run.id, "expired-token", "无效租约不可发布")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "block",
    [
        {
            "type": "reasoning",
            "reasoning": "公开摘要",
        },
        {
            "type": "thinking",
            "thinking": "公开摘要",
            "signature": "不得公开签名",
        },
    ],
)
def test_standard_reasoning_blocks_only_extract_text(block: dict) -> None:
    """标准和供应商思考块只提取文字，不混入签名、工具参数和正文。"""
    message = AIMessageChunk(
        content=[
            block,
            {
                "type": "text",
                "text": "正文",
            },
        ]
    )
    assert public_reasoning_text(message) == "公开摘要"
    assert (
        public_reasoning_text(
            AIMessageChunk(
                content="普通回答",
                additional_kwargs={
                    "encrypted_content": "密文",
                },
            )
        )
        == ""
    )


def test_call_timing_mcp_and_missing_usage(tmp_path: Path) -> None:
    """分别计算调用耗时，缺失开始事件不推算，终态时间不受后续更新时间干扰。"""

    async def exercise() -> None:
        """以确定性时间和固定工具关联验证汇总及所有权边界。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'timing.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        origin = shanghai_now(naive=True)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="reader", password="not-used"))
                run = await create_run(db, 1)
                run.create_time = origin
                run.update_time = origin + timedelta(hours=1)
                run.state = RunState.COMPLETED
                tool = UserTool(id=1, owner_id=1, name="lookup", description="检索", kind=ToolKind.MCP, visibility=Visibility.PRIVATE)
                db.add_all([tool, RunTool(run_id=run.id, tool_id=1, tool_version=1, content_hash="x" * 64)])
                call_id = str(uuid4())
                rows = [
                    (RunEventType.TOOL_STARTED, "user_tool_1_lookup", call_id, 1),
                    (RunEventType.TOOL_COMPLETED, "user_tool_1_lookup", call_id, 3.5),
                    (RunEventType.TOOL_FAILED, "skill:report@v1", str(uuid4()), 4),
                    (RunEventType.STATE, None, None, 10),
                ]
                for sequence, (kind, name, identifier, seconds) in enumerate(rows, start=2):
                    db.add(
                        AgentRunEvent(
                            run_id=run.id,
                            sequence=sequence,
                            event_type=kind,
                            state=RunState.COMPLETED if kind == RunEventType.STATE else RunState.RUNNING,
                            tool_name=name,
                            tool_call_id=identifier,
                            create_time=origin + timedelta(seconds=seconds),
                        )
                    )
            async with sessions() as db:
                calls = (await load_run_calls(db, 1, [run.id]))[run.id]
                assert calls[0].kind == RunCallKind.MCP
                assert calls[0].elapsed_ms == 2500
                assert calls[1].elapsed_ms is None
                assert calls[1].started_at is None
                assert await load_run_calls(db, 2, [run.id]) == {}
                metrics = (await load_run_presentation(db, 1, [run.id]))[run.id]["metrics"]
                assert metrics == {
                    "elapsed_ms": 10000,
                    "total_tokens": None,
                }
                assert (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id))).all()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("state", [RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED, RunState.COMPLETED, RunState.RUNNING])
def test_failed_public_text_is_restored_without_success_message(tmp_path: Path, state: RunState) -> None:
    """失败、超时和取消保留已公开正文，成功与进行中不生成部分回答，跨用户不能读取。"""

    async def exercise() -> None:
        """通过正式正文事件恢复展示，不创建助手成功消息或读取私有检查点。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="请审查")
                run = await claim_run(db, run.id)
                await append_text_delta(db, run.id, run.lease_token, "已经检查第一节。")
                await append_reasoning_delta(db, run.id, run.lease_token, "先核对依据。")
                await append_text_delta(db, run.id, run.lease_token, "第二节尚未完成。")
                run.state = state
                run.failure_code = RunModelFailureReason.OUTPUT_LIMIT if state == RunState.FAILED else None
            async with sessions() as db:
                data = (await load_run_presentation(db, 1, [run.id]))[run.id]
                expected = "已经检查第一节。第二节尚未完成。" if state in {RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED} else ""
                assert data["partial_content"] == expected
                assert data["run_state"] == state
                assert data["failure_code"] == run.failure_code
                assert data["reasoning_content"] == "先核对依据。"
                assert await load_run_presentation(db, 2, [run.id]) == {}
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_session_http_restores_failed_text_from_public_events(tmp_path: Path) -> None:
    """正式历史接口在用户消息上附带未完成正文，刷新可恢复且不伪造助手消息或跨用户访问。"""
    from decimal import Decimal

    from app.core.security import get_current_admin
    from app.db.session import get_db_session
    from app.main import app
    from app.models.agent_config import AgentConfig
    from app.models.agent_session import AgentSession
    from app.services.run_manager import fail_run

    async def exercise() -> None:
        """使用隔离身份依赖和数据库验证 HTTP 契约、游标及权限，不读取生产数据。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'partial-http.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="partial-owner", password="unused")

        async def current_actor():
            """提供可切换的测试用户，授权仍由正式会话服务校验。"""
            return actor

        async def database():
            """将正式请求连接到隔离数据库。"""
            async with sessions() as db:
                yield db

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(actor)
                db.add(AgentConfig(id=11, owner_id=1, name="审查助手", default_model_id=1))
                db.add(AgentSession(id="partial-http", owner_id=1, agent_id=11, thread_id="partial-http-thread", title="失败内容保留"))
                run = await create_run(
                    db,
                    1,
                    input_text="请审查",
                    agent_id=11,
                    session_id="partial-http",
                    thread_id="partial-http-thread",
                    agent_config_version=1,
                    agent_system_prompt="",
                    agent_temperature=Decimal("0.5"),
                    agent_max_output_tokens=None,
                )
                run = await claim_run(db, run.id)
                await append_text_delta(db, run.id, run.lease_token, "此前公开的分析，尚未完成。")
                await fail_run(db, run.id, run.lease_token, RunModelFailureReason.OUTPUT_LIMIT)
            app.dependency_overrides[get_db_session] = database
            app.dependency_overrides[get_current_admin] = current_actor
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = "/api/v1/agents/11/sessions/partial-http/messages"
                response = await client.get(path)
                assert response.status_code == 200
                rows = response.json()["data"]
                assert len(rows) == 1 and rows[0]["role"] == "user" and rows[0]["content"] == "请审查"
                assert rows[0]["partial_content"] == "此前公开的分析，尚未完成。"
                assert rows[0]["run_state"] == RunState.FAILED and rows[0]["failure_code"] == RunModelFailureReason.OUTPUT_LIMIT
                assert (await client.get(path)).json()["data"][0]["partial_content"] == rows[0]["partial_content"]
                cursor = {
                    "after": rows[0]["id"],
                }
                assert (await client.get(path, params=cursor)).json()["data"] == []
                actor = Admin(id=2, username="other", password="unused")
                assert (await client.get(path)).status_code == 404
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_current_admin, None)
            await engine.dispose()

    asyncio.run(exercise())
