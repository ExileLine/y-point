# -*- coding: utf-8 -*-
# @Time    : 2026-11-17 10:16:57
# @Author  : yangyuexiong
# @File    : test_ag_ui.py

"""验证 AG-UI 1.0 线契约、真实事件事务、审批续跑与恢复授权。"""

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import ag_ui as ag_ui_api
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_session import AgentSession
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.ag_ui_projection import RunProjection, load_projection
from app.services.run_manager import (
    append_model_boundary,
    append_reasoning_delta,
    append_text_delta,
    append_tool_event,
    claim_run,
    complete_run,
    create_approval_request,
    create_run,
    decide_approval,
    fail_run,
    resume_approved_run,
)

SCHEMA = json.loads((Path(__file__).parent / "fixtures/ag-ui/1.0.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def validate_frames(frames: list[dict]) -> list[dict]:
    """直接对官方冻结 JSON Schema 验证实际线事件，不只验证自定义模型。"""
    assert [frame["sequence"] for frame in frames] == list(range(1, len(frames) + 1))
    for frame in frames:
        VALIDATOR.validate(frame["event"])
    return [frame["event"] for frame in frames]


def sse_events(text: str) -> list[dict]:
    """读取 SSE 数据帧，忽略注释和项目命名资源通知。"""
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@asynccontextmanager
async def environment(tmp_path: Path):
    """使用独立数据库与真实登录，避免触碰本地业务数据或真实供应商。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ag-ui.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as db:
        db.add(Admin(id=1, username="ag-ui-user", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0))
        db.add(Admin(id=2, username="ag-ui-other", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0))
        db.add(
            ProviderConnection(
                id=1,
                name="ag-ui",
                name_key="ag-ui",
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
                external_id="test",
                display_name="测试模型",
                capabilities=["text", "tools"],
                verified_capabilities=["text", "tools"],
                context_window=8192,
                creator_id=1,
                status=1,
                is_deleted=0,
            )
        )
        db.add(AgentConfig(id=1, owner_id=1, name="协议助手", default_model_id=1, system_prompt=""))
        db.add(AgentSession(id="session", owner_id=1, agent_id=1, thread_id="checkpoint-thread", title="协议会话"))

    async def database():
        """将应用的数据库依赖替换为本测试独立会话。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = database
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "ag-ui-user",
                    "password": "StrongPassword123",
                },
            )
            headers = {
                "Authorization": f"Bearer {login.json()['data']['access_token']}",
            }
            yield sessions, client, headers
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        await engine.dispose()


def test_segmented_projection_and_official_schema(tmp_path: Path) -> None:
    """两次模型调用、并行工具和脱敏失败均产生稳定且符合规范的事件。"""

    async def exercise() -> None:
        """在持久化事件源上验证确定性与工具参数结束和执行结果的区别。"""
        async with environment(tmp_path) as (sessions, _, _):
            async with sessions.begin() as db:
                run = await create_run(
                    db,
                    1,
                    input_text="执行",
                    session_id="session",
                    agent_id=1,
                    agent_config_version=1,
                    thread_id="checkpoint-thread",
                    agent_system_prompt="",
                    agent_temperature=0.1,
                    agent_max_output_tokens=None,
                    model_id=1,
                )
                claimed = await claim_run(db, run.id)
                token = claimed.lease_token
                await append_model_boundary(db, run.id, token, "model-1")
                await append_reasoning_delta(db, run.id, token, "上游公开摘要")
                await append_text_delta(db, run.id, token, "先检索资料")
                await append_model_boundary(db, run.id, token, "model-1", finished=True)
                first, second = str(uuid4()), str(uuid4())
                await append_tool_event(db, run.id, token, "tool_started", first, "knowledge_search")
                await append_tool_event(db, run.id, token, "tool_started", second, "skill:审查@v2")
                await append_tool_event(db, run.id, token, "tool_failed", first, "knowledge_search")
                await append_tool_event(db, run.id, token, "tool_completed", second, "skill:审查@v2")
                await append_model_boundary(db, run.id, token, "model-2")
                await append_reasoning_delta(db, run.id, token, "第二段公开摘要")
                await append_text_delta(db, run.id, token, "最终回答")
                await complete_run(db, run.id, token, final_text="最终回答")
                projection, frames = await load_projection(db, run)
                _, repeated = await load_projection(db, run)
                assert repeated == frames
                events = validate_frames(frames)
                reasoning_ids = [event["messageId"] for event in events if event["type"] == "REASONING_MESSAGE_START"]
                assert len(set(reasoning_ids)) == 2
                tool_end = next(index for index, event in enumerate(events) if event["type"] == "TOOL_CALL_END")
                result = next(index for index, event in enumerate(events) if event["type"] == "TOOL_CALL_RESULT")
                assert tool_end < result
                assert events[-1]["outcome"]["type"] == "success" and projection.open is False
                assert [event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"] == ["先检索资料", "最终回答"]
                assert (
                    next(event["value"]["messageId"] for event in events if event["type"] == "CUSTOM" and event["name"] == "y-point.answer")
                    == projection.last_text_id
                )
                assert all("rawEvent" not in event for event in events)
                source = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id).order_by(AgentRunEvent.sequence))).all()
                restarted = RunProjection(run)
                rebuilt = []
                for event in source:
                    rebuilt.extend(restarted.feed(event))
                assert rebuilt == frames

    asyncio.run(exercise())


def test_approval_attempts_and_failure_are_durable(tmp_path: Path) -> None:
    """审批结束旧协议运行；同一业务运行恢复后生成新 ID，失败只发错误终态。"""

    async def exercise() -> None:
        """重复重建审批链，确认 ID、消息与预算计数不会因连接重放变化。"""
        async with environment(tmp_path) as (sessions, _, _):
            async with sessions.begin() as db:
                run = await create_run(
                    db,
                    1,
                    input_text="审批",
                    session_id="session",
                    agent_id=1,
                    agent_config_version=1,
                    thread_id="checkpoint-thread",
                    agent_system_prompt="",
                    agent_temperature=0.1,
                    agent_max_output_tokens=None,
                )
                claimed = await claim_run(db, run.id)
                approval = await create_approval_request(
                    db,
                    run.id,
                    claimed.lease_token,
                    "checkpoint-1",
                    [
                        {
                            "name": "write_report",
                            "arguments_hash": "a" * 64,
                        },
                    ],
                )
                await decide_approval(db, run.id, 1, "approve", expected_approval_id=approval.id)
                approval.ag_ui_run_id = "resumed-protocol-run"
                resumed = await resume_approved_run(db, run.id)
                await append_reasoning_delta(db, run.id, resumed.lease_token, "允许公开的摘要")
                await fail_run(db, run.id, resumed.lease_token, "model_error")
                _, frames = await load_projection(db, run)
                events = validate_frames(frames)
                starts = [event for event in events if event["type"] == "RUN_STARTED"]
                assert len(starts) == 2 and starts[0]["runId"] != starts[1]["runId"]
                assert starts[1]["runId"] == "resumed-protocol-run" and "parentRunId" not in starts[1]
                finished = [event for event in events if event["type"] == "RUN_FINISHED"]
                assert len(finished) == 1 and finished[0]["outcome"]["interrupts"][0]["id"] == approval.id
                assert events[-1]["type"] == "RUN_ERROR"
                assert run.model_call_count == run.tool_call_count == 0

    asyncio.run(exercise())


def test_snapshot_paging_and_stream_replay(tmp_path: Path) -> None:
    """快照固定上界，随后 SSE 从线游标无缝续传；跨用户请求被拒绝。"""

    async def exercise() -> None:
        """通过真实 FastAPI 响应验证事件分页、Last-Event-ID 与命名空间游标。"""
        async with environment(tmp_path) as (sessions, client, headers):
            async with sessions.begin() as db:
                run = await create_run(
                    db,
                    1,
                    input_text="快照",
                    session_id="session",
                    agent_id=1,
                    agent_config_version=1,
                    thread_id="checkpoint-thread",
                    agent_system_prompt="",
                    agent_temperature=0.1,
                    agent_max_output_tokens=None,
                )
                claimed = await claim_run(db, run.id)
                await append_text_delta(db, run.id, claimed.lease_token, "第一段")
            base = f"/api/v1/runs/{run.id}"
            page = (await client.get(f"{base}/ag-ui/snapshot?limit=2", headers=headers)).json()["data"]
            through = page["through"]
            frames = page["events"]
            async with sessions.begin() as db:
                await append_text_delta(db, run.id, claimed.lease_token, "第二段")
                await complete_run(db, run.id, claimed.lease_token, final_text="第一段第二段")
            while page["has_more"]:
                page = (await client.get(f"{base}/ag-ui/snapshot?after={page['next_after']}&through={through}&limit=2", headers=headers)).json()["data"]
                frames.extend(page["events"])
            assert all(event["event"].get("delta") != "第二段" for event in frames)
            response = await client.get(
                f"{base}/stream",
                headers={
                    **headers,
                    "Last-Event-ID": f"{run.id}:{page['next_after']}",
                },
            )
            events = sse_events(response.text)
            assert response.status_code == 200 and events[-1]["type"] == "RUN_FINISHED"
            assert next(event for event in events if event["type"] == "TEXT_MESSAGE_CONTENT")["delta"] == "第二段"
            for event in events:
                VALIDATOR.validate(event)
            assert (await client.get(f"{base}/stream?after=999999", headers=headers)).status_code == 400
            assert (
                await client.get(
                    f"{base}/stream",
                    headers={
                        **headers,
                        "Last-Event-ID": "other:1",
                    },
                )
            ).status_code == 400
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "ag-ui-other",
                    "password": "StrongPassword123",
                },
            )
            other = {
                "Authorization": f"Bearer {login.json()['data']['access_token']}",
            }
            assert (await client.get(f"{base}/ag-ui/snapshot", headers=other)).status_code == 404
            assert (await client.get(f"{base}/stream", headers=other)).status_code == 404

    asyncio.run(exercise())


@pytest.mark.parametrize("decision", ["approve", "reject_continue", "resolve"])
def test_standard_post_and_resume_preserve_public_history(tmp_path: Path, monkeypatch, decision: str) -> None:
    """标准入口提交、审批续跑及幂等历史校验都复用正式业务事务。"""

    async def exercise() -> None:
        """使用可控执行器验证协议边界，不调用真实模型或执行外部写入。"""
        async with environment(tmp_path) as (sessions, client, headers):
            original_submit = ag_ui_api.create_session_run
            original_stream = ag_ui_api.build_run_stream
            submitted = []

            async def submit(*args, **kwargs):
                """正式提交后模拟 Worker 领取并进入审批。"""
                run = await original_submit(*args, **kwargs)
                if not submitted:
                    async with sessions.begin() as db:
                        claimed = await claim_run(db, run.id)
                        await create_approval_request(
                            db,
                            run.id,
                            claimed.lease_token,
                            "approval-checkpoint",
                            [
                                {
                                    "name": "write_report",
                                    "arguments_hash": "a" * 64,
                                    "action_id": "bound-write-action",
                                },
                            ],
                        )
                submitted.append(run.id)
                return run

            async def stream(run_id, request, actor, db, **kwargs):
                """标准恢复批准后由独立数据库会话完成后半轮。"""
                if kwargs.get("after", 0):
                    async with sessions.begin() as worker_db:
                        claimed = await resume_approved_run(worker_db, run_id)
                        await append_text_delta(worker_db, run_id, claimed.lease_token, "审批后完成")
                        await complete_run(worker_db, run_id, claimed.lease_token, final_text="审批后完成")
                await db.rollback()
                await db.refresh(actor)
                return await original_stream(run_id, request, actor, db, **kwargs)

            monkeypatch.setattr(ag_ui_api, "create_session_run", submit)
            monkeypatch.setattr(ag_ui_api, "build_run_stream", stream)
            body = {
                "threadId": "session",
                "runId": "client-run-1",
                "protocolVersion": "1.0",
                "forwardedProps": {
                    "require_mcp_approval": True,
                },
                "messages": [
                    {
                        "id": "client-user-1",
                        "role": "user",
                        "content": "请写报告",
                    },
                ],
            }
            endpoint = "/api/v1/agents/1/ag-ui"
            response = await client.post(endpoint, headers=headers, json=body)
            assert response.status_code == 200, response.text
            first_events = sse_events(response.text)
            assert first_events[0]["runId"] == "client-run-1"
            interrupt = first_events[-1]["outcome"]["interrupts"][0]["id"]
            retry = await client.post(endpoint, headers=headers, json=body)
            assert retry.status_code == 200 and submitted[0] == submitted[1]
            async with sessions() as db:
                fixed = await db.get(AgentRun, submitted[0])
                assert fixed.require_mcp_approval is True
            mismatched = await client.post(
                endpoint,
                headers=headers,
                json={
                    **body,
                    "forwardedProps": {
                        "require_mcp_approval": False,
                    },
                },
            )
            assert mismatched.status_code == 409
            history = (await client.get(f"{endpoint}/threads/session", headers=headers)).json()["data"]["messages"]
            assert history[0]["id"] == "client-user-1"
            resumed = await client.post(
                endpoint,
                headers=headers,
                json={
                    "threadId": "session",
                    "runId": "client-run-2",
                    "messages": history,
                    "resume": [
                        {
                            "interruptId": interrupt,
                            "status": "resolved",
                            "payload": {
                                "decision": decision,
                                **({"decisions": [{"action_id": "bound-write-action", "decision": "reject"}]} if decision == "resolve" else {}),
                            },
                        },
                    ],
                },
            )
            assert resumed.status_code == 200, resumed.text
            resumed_events = sse_events(resumed.text)
            assert resumed_events[0]["type"] == "RUN_STARTED" and resumed_events[0]["runId"] == "client-run-2"
            assert resumed_events[-1]["outcome"]["type"] == "success"
            for event in first_events + resumed_events:
                VALIDATOR.validate(event)
            final_history = (await client.get(f"{endpoint}/threads/session", headers=headers)).json()["data"]["messages"]
            final_id = next(event["value"]["messageId"] for event in resumed_events if event["type"] == "CUSTOM" and event["name"] == "y-point.answer")
            assert next(message for message in final_history if message["id"] == final_id)["content"].startswith("审批后完成")
            invalid = await client.post(
                endpoint,
                headers=headers,
                json={
                    **body,
                    "runId": "stale-history",
                },
            )
            assert invalid.status_code == 409
            invalid = await client.post(
                endpoint,
                headers=headers,
                json={
                    **body,
                    "state": {
                        "admin": True,
                    },
                },
            )
            assert invalid.status_code == 409
            async with sessions() as db:
                runs = (await db.scalars(select(AgentRun))).all()
                assert len(runs) == 1 and runs[0].thread_id == "checkpoint-thread"

    asyncio.run(exercise())


def test_actual_deepagents_stream_conforms(tmp_path: Path) -> None:
    """真实 DeepAgents/LangGraph 流接入受控模型后，消息边界及最终身份仍符合规范。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from app.services.run_executor import execute_text_run

    class Model(FakeMessagesListChatModel):
        """提供无网络、无付费调用的模型响应。"""

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            """允许真实 DeepAgents 构建工具绑定模型。"""
            return self

    async def exercise() -> None:
        """执行真实运行时，再对持久化源事件的投影做独立 Schema 校验。"""
        async with environment(tmp_path) as (sessions, _, _):
            async with sessions.begin() as db:
                run = await create_run(db, 1, model_id=1, input_text="请回答")
            completed = await execute_text_run(
                sessions,
                run.id,
                Model(responses=[AIMessage(content="真实运行时测试回答")]),
                tmp_path / "checkpoints.db",
                lease_seconds=2,
                poll_seconds=0.1,
            )
            assert completed.state == "completed"
            async with sessions() as db:
                fresh = await db.get(AgentRun, run.id)
                _, frames = await load_projection(db, fresh)
                events = validate_frames(frames)
                assert events[-1]["outcome"]["type"] == "success"
                sources = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id))).all()
                assert {event.event_type for event in sources} >= {"model_started", "model_finished", "delivery_completed"}
                assert [event["delta"] for event in events if event["type"] == "TEXT_MESSAGE_CONTENT"] == ["真实运行时测试回答"]

    asyncio.run(exercise())


def test_unknown_external_write_and_cancellation(tmp_path: Path) -> None:
    """外部写入未确认和取消均不伪造成功结果，参数生成结束不决定执行状态。"""
    from app.core.enums import RunCallFailure, RunCallKind
    from app.services.run_manager import acknowledge_cancel, request_cancel

    async def exercise() -> None:
        """在两个真实调用跨度中验证未知结果及被取消的在途工具。"""
        async with environment(tmp_path) as (sessions, _, _):
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="外部写入")
                claimed = await claim_run(db, run.id)
                first, second = str(uuid4()), str(uuid4())
                await append_tool_event(
                    db,
                    run.id,
                    claimed.lease_token,
                    "tool_started",
                    first,
                    "user_tool_1_write",
                    call_kind=RunCallKind.MCP,
                    display_name="外部工单写入",
                )
                await append_tool_event(
                    db,
                    run.id,
                    claimed.lease_token,
                    "tool_failed",
                    first,
                    "user_tool_1_write",
                    failure_code=RunCallFailure.RESULT_UNKNOWN,
                )
                await append_tool_event(db, run.id, claimed.lease_token, "tool_started", second, "read_file")
                await request_cancel(db, run.id, 1)
                await acknowledge_cancel(db, run.id, claimed.lease_token)
                _, frames = await load_projection(db, run)
                events = validate_frames(frames)
                activities = [event["content"] for event in events if event["type"] == "ACTIVITY_SNAPSHOT" and event["activityType"] == "y-point.call"]
                assert activities[0]["name"] == "外部工单写入" and activities[0]["kind"] == "mcp"
                assert activities[-1]["status"] == "interrupted"
                assert next(activity for activity in activities if activity.get("failureCode"))["status"] == "interrupted"
                assert events[-1]["outcome"]["type"] == "cancelled"
                results = [event for event in events if event["type"] == "TOOL_CALL_RESULT"]
                assert len(results) == 1 and json.loads(results[0]["content"])["status"] == "interrupted"

    asyncio.run(exercise())


def test_partial_answer_keeps_only_last_model_segment(tmp_path: Path) -> None:
    """失败历史将中间说明留在过程内，仅把最后模型片段作为未完成回答。"""
    from app.services.run_presentation import load_run_presentation

    async def exercise() -> None:
        """跨工具和模型边界写入公开文本，验证历史不会再次拼接全部中间说明。"""
        async with environment(tmp_path) as (sessions, _, _):
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="多步回答")
                claimed = await claim_run(db, run.id)
                await append_model_boundary(db, run.id, claimed.lease_token, "first")
                await append_text_delta(db, run.id, claimed.lease_token, "我先读取资料。")
                tool_id = str(uuid4())
                await append_tool_event(db, run.id, claimed.lease_token, "tool_started", tool_id, "read_file")
                await append_tool_event(db, run.id, claimed.lease_token, "tool_completed", tool_id, "read_file")
                await append_model_boundary(db, run.id, claimed.lease_token, "second")
                await append_text_delta(db, run.id, claimed.lease_token, "结论尚未生成完整")
                await fail_run(db, run.id, claimed.lease_token, "model_error")
                presentation = await load_run_presentation(db, 1, [run.id])
                assert presentation[run.id]["partial_content"] == "结论尚未生成完整"

    asyncio.run(exercise())


def test_standard_cancelled_resume_has_its_own_run(tmp_path: Path) -> None:
    """标准中断取消也开启新的协议运行，并以 cancelled 结束，不混入观察扩展帧。"""

    async def exercise() -> None:
        """使用真实审批服务完成取消，不在测试中替换流或审批事务。"""
        async with environment(tmp_path) as (sessions, client, headers):
            async with sessions.begin() as db:
                run = await create_run(
                    db,
                    1,
                    input_text="需要审批",
                    session_id="session",
                    agent_id=1,
                    agent_config_version=1,
                    thread_id="checkpoint-thread",
                    agent_system_prompt="",
                    agent_temperature=0.1,
                    agent_max_output_tokens=None,
                )
                claimed = await claim_run(db, run.id)
                approval = await create_approval_request(
                    db,
                    run.id,
                    claimed.lease_token,
                    "cancel-checkpoint",
                    [
                        {
                            "name": "write_report",
                            "arguments_hash": "b" * 64,
                        },
                    ],
                )
            endpoint = "/api/v1/agents/1/ag-ui"
            history = (await client.get(f"{endpoint}/threads/session", headers=headers)).json()["data"]["messages"]
            body = {
                "threadId": "session",
                "runId": "cancel-resume",
                "messages": history,
                "resume": [
                    {
                        "interruptId": approval.id,
                        "status": "cancelled",
                    },
                ],
            }
            response = await client.post(endpoint, headers=headers, json=body)
            assert response.status_code == 200, response.text
            events = sse_events(response.text)
            assert events[0]["type"] == "RUN_STARTED" and events[0]["runId"] == "cancel-resume"
            assert events[-1]["outcome"]["type"] == "cancelled"
            for event in events:
                VALIDATOR.validate(event)
            retry = await client.post(endpoint, headers=headers, json=body)
            assert retry.status_code == 200 and sse_events(retry.text) == events
            assert "event: y-point.run-state" not in response.text
            capabilities = (await client.get(f"{endpoint}/capabilities", headers=headers)).json()
            assert capabilities["transport"]["streaming"] is True and capabilities["tools"]["clientProvided"] is False

    asyncio.run(exercise())


def test_standard_retry_recovers_a_concurrent_submission_conflict(tmp_path: Path, monkeypatch) -> None:
    """唯一协议入口在创建事务冲突后返回原运行，不重复保存用户消息或递增会话版本。"""
    from sqlalchemy.exc import IntegrityError
    from app.api.v1 import agent_sessions as sessions_api
    from tests.ag_ui_helpers import submit_agent_run

    async def exercise() -> None:
        """保留真实授权和终态标准流，仅模拟并发唯一约束竞争。"""
        async with environment(tmp_path) as (sessions, client, headers):
            accepted = await submit_agent_run(
                client,
                "/api/v1/agents/1/sessions/session/runs",
                headers=headers,
                json={
                    "request_key": "race",
                    "input_text": "相同输入",
                },
            )
            assert accepted.status_code == 201
            run_id = accepted.json()["data"]["id"]
            assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=headers)).status_code == 200
            thread = (await client.get("/api/v1/agents/1/ag-ui/threads/session", headers=headers)).json()["data"]
            user_message = next(message for message in thread["messages"] if message["role"] == "user")
            original = sessions_api._matching_retry
            calls = 0

            async def lookup(*args, **kwargs):
                """首次查询模拟未看到并发事务，冲突后再读取已提交的原请求。"""
                nonlocal calls
                calls += 1
                return None if calls == 1 else await original(*args, **kwargs)

            async def collision(*args, **kwargs):
                """在事务已更新会话版本之后模拟唯一约束冲突。"""
                raise IntegrityError("INSERT", {}, Exception("concurrent request"))

            monkeypatch.setattr(sessions_api, "_matching_retry", lookup)
            monkeypatch.setattr(sessions_api, "create_run", collision)
            response = await client.post(
                "/api/v1/agents/1/ag-ui",
                headers=headers,
                json={
                    "threadId": "session",
                    "runId": "race",
                    "messages": [user_message],
                },
            )
            assert response.status_code == 200, response.text
            assert response.headers["X-Y-Point-Run-ID"] == run_id and response.headers["X-Y-Point-After"] == "0"
            assert sse_events(response.text)[-1]["outcome"]["type"] == "cancelled"
            assert calls == 2
            async with sessions() as db:
                assert len((await db.scalars(select(AgentRun))).all()) == 1
                assert (await db.get(AgentSession, "session")).version == thread["version"]

    asyncio.run(exercise())
