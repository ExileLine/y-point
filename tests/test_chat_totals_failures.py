# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 22:56:06
# @Author  : yangyuexiong
# @File    : test_chat_totals_failures.py

"""验证调用失败原因落库脱敏、整个会话汇总和进行中轮次的计时边界。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import RunCallFailure, RunEventType, RunState
from app.models.admin import Admin
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.base import Base, shanghai_now
from app.services.run_call_failures import call_failure_message, tool_exception_failure, tool_result_failure
from app.services.run_calls import load_run_calls
from app.services.run_executor import AuthorizedToolsMiddleware, ToolExecutionTimeoutError
from app.services.run_manager import append_tool_event, claim_run, create_run
from app.services import run_presentation
from app.services.run_presentation import load_session_metrics
from app.services.script_sandbox import SandboxConfigurationError
from app.services.tool_arguments import ToolArgumentsError


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("private-token"), RunCallFailure.TIMEOUT),
        (PermissionError("private-token"), RunCallFailure.PERMISSION_DENIED),
        (ConnectionError("private-token"), RunCallFailure.CONNECTION_FAILED),
        (SandboxConfigurationError("private-token"), RunCallFailure.CONFIGURATION_ERROR),
        (ToolArgumentsError("private-token"), RunCallFailure.INVALID_ARGUMENTS),
        (ValueError("private-token"), RunCallFailure.EXECUTION_ERROR),
        (ValueError("脚本执行失败"), RunCallFailure.SCRIPT_FAILED),
        (ValueError("脚本必须输出一个 JSON 值"), RunCallFailure.INVALID_OUTPUT),
        (ValueError("MCP 结果超过 64 KiB"), RunCallFailure.RESPONSE_LIMIT),
        (ValueError("MCP 工具结构已变化，请发布新版本"), RunCallFailure.TOOL_CHANGED),
        (ValueError("MCP 工具执行失败"), RunCallFailure.REMOTE_ERROR),
        (asyncio.CancelledError("private-token"), RunCallFailure.INTERRUPTED),
        (ExceptionGroup("private-token", [TimeoutError("secret")]), RunCallFailure.TIMEOUT),
    ],
)
def test_failure_categories_never_expose_exception(failure: BaseException, expected: RunCallFailure) -> None:
    """根据异常类型归类，异常正文与未知历史原因码都不进入公开文案。"""
    assert tool_exception_failure(failure) == expected
    assert "private-token" not in call_failure_message(expected)
    assert "private-token" not in call_failure_message("private-token")


@pytest.mark.parametrize("status, expected", [(401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (503, "remote_error")])
def test_remote_http_failure_without_private_url(status: int, expected: str) -> None:
    """HTTP 错误只展示认证、限流或服务异常类别，私有地址与凭据不外传。"""
    request = httpx.Request("GET", "https://private.invalid/?token=secret")
    response = httpx.Response(status, request=request)
    with pytest.raises(httpx.HTTPStatusError) as failure:
        response.raise_for_status()
    assert tool_exception_failure(failure.value) == expected
    assert "private.invalid" not in call_failure_message(expected)


@pytest.mark.parametrize("mode", ["exception", "error-result", "script-rejected", "unknown-result", "timeout"])
def test_failure_event_and_summary_roundtrip(tmp_path: Path, mode: str) -> None:
    """通过真实工具中间件保存异常与错误结果的原因，API 摘要不泄露输入或输出。"""

    async def exercise() -> None:
        """使用独立数据库验证失败事件、公共摘要和受控原因码的写入约束。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="owner", password="unused"))
                run = await create_run(db, 1)
            async with sessions.begin() as db:
                claimed = await claim_run(db, run.id, lease_seconds=30)
            middleware = AuthorizedToolsMiddleware({"fixture_tool"}, sessions, run.id, claimed.lease_token, 1, None, None, None, None)
            payload = {
                "error": "script_input_rejected" if mode == "script-rejected" else "invalid_tool_arguments",
                "message": "private-output",
                "retryable": True,
            }

            async def handler(request):
                """返回受控输入错误或含敏感正文的异常，均不能直接公开。"""
                if mode == "exception":
                    raise PermissionError("private-token")
                if mode == "timeout":
                    await asyncio.sleep(10)
                return ToolMessage(
                    content="private-output" if mode == "unknown-result" else json.dumps(payload),
                    status="error",
                    tool_call_id=request.tool_call["id"],
                )

            request = ToolCallRequest(
                tool_call={
                    "name": "fixture_tool",
                    "id": "model-call",
                    "args": {
                        "secret": "private-input",
                    },
                },
                tool=None,
                state={},
                runtime=None,
            )
            if mode == "exception":
                with pytest.raises(PermissionError):
                    await middleware._execute_authorized_tool_call(request, handler)
                expected = RunCallFailure.PERMISSION_DENIED
            elif mode == "timeout":
                with pytest.raises(ToolExecutionTimeoutError):
                    await middleware._execute_authorized_tool_call(request, handler)
                expected = RunCallFailure.TIMEOUT
            else:
                await middleware._execute_authorized_tool_call(request, handler)
                expected = tool_result_failure("private-output" if mode == "unknown-result" else json.dumps(payload))
            async with sessions.begin() as db:
                calls = (await load_run_calls(db, 1, [run.id]))[run.id]
                assert len(calls) == 1 and calls[0].status == "failed"
                assert calls[0].failure_code == expected
                assert calls[0].failure_reason == call_failure_message(expected)
                assert calls[0].elapsed_ms is not None
                dumped = calls[0].model_dump_json()
                assert all(secret not in dumped for secret in ["private-token", "private-input", "private-output"])
                with pytest.raises(ValueError, match="不允许的工具失败原因"):
                    await append_tool_event(
                        db, run.id, claimed.lease_token, RunEventType.TOOL_FAILED, calls[0].call_id, "fixture_tool", failure_code="private-token"
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_entire_session_metrics_include_all_rounds(tmp_path: Path, monkeypatch) -> None:
    """统计超过一页消息与运行的全部轮次，去重用户和助手消息并保留未知历史边界。"""

    async def exercise() -> None:
        """构造成功、失败、取消、进行中及缺少终态的轮次，检查聚合与用户隔离。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'totals.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        now = shanghai_now().replace(microsecond=0)
        monkeypatch.setattr(run_presentation, "shanghai_now", lambda: now)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                for index in range(109):
                    run = await create_run(db, 1, input_text="question")
                    run.agent_id = 11
                    run.session_id = "entire-session"
                    run.create_time = now.replace(tzinfo=None) - timedelta(seconds=10)
                    run.reported_total_tokens = 10 if index < 105 else 20 if index == 105 else 5 if index == 107 else 0
                    run.state = RunState.COMPLETED if index < 105 else [RunState.FAILED, RunState.CANCELLED, RunState.RUNNING, RunState.FAILED][index - 105]
                    if index < 107:
                        duration = 2 if index < 105 else 3 if index == 105 else 4
                        db.add(
                            AgentRunEvent(
                                run_id=run.id,
                                sequence=10,
                                event_type=RunEventType.STATE,
                                state=run.state,
                                create_time=run.create_time + timedelta(seconds=duration),
                            ),
                        )
                        db.add(
                            AgentRunEvent(
                                run_id=run.id,
                                sequence=11,
                                event_type=RunEventType.STATE,
                                state=run.state,
                                create_time=run.create_time + timedelta(seconds=duration + 1),
                            ),
                        )
                    if index == 107:
                        active_id = run.id
                    if index < 105:
                        db.add(AgentRunMessage(run_id=run.id, owner_id=1, role="assistant", content="answer"))
                foreign = await create_run(db, 2)
                foreign.agent_id, foreign.session_id, foreign.reported_total_tokens = 11, "entire-session", 999999
                wrong_agent = await create_run(db, 1)
                wrong_agent.agent_id, wrong_agent.session_id, wrong_agent.reported_total_tokens = 12, "entire-session", 999999
            async with sessions() as db:
                totals = await load_session_metrics(db, 1, 11, "entire-session")
                assert totals.run_count == 109
                assert totals.active_run_count == 1 and totals.missing_duration_runs == 1
                assert totals.elapsed_ms == 227000 and totals.total_tokens == 1075
                empty = await load_session_metrics(db, 1, 11, "empty-session")
                assert empty.elapsed_ms == 0 and empty.total_tokens == 0 and empty.run_count == 0
                unknown = await load_session_metrics(db, 99, 11, "entire-session")
                assert unknown.run_count == 0
            async with sessions.begin() as db:
                unreported = await create_run(db, 1)
                unreported.agent_id, unreported.session_id, unreported.state = 11, "unreported-session", RunState.FAILED
            async with sessions() as db:
                unknown = await load_session_metrics(db, 1, 11, "unreported-session")
                assert unknown.total_tokens is None and unknown.missing_duration_runs == 1
            async with sessions.begin() as db:
                active = await db.get(AgentRun, active_id)
                active.state = RunState.COMPLETED
                active.reported_total_tokens = 7
                db.add(
                    AgentRunEvent(
                        run_id=active_id,
                        sequence=10,
                        event_type=RunEventType.STATE,
                        state=active.state,
                        create_time=now.replace(tzinfo=None) + timedelta(seconds=1),
                    )
                )
            async with sessions() as db:
                totals = await load_session_metrics(db, 1, 11, "entire-session")
                assert totals.elapsed_ms == 228000 and totals.total_tokens == 1077
                assert totals.active_run_count == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())
