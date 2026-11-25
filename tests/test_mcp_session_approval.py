# -*- coding: utf-8 -*-
# @Time    : 2026-11-25 09:00:14
# @Author  : yangyuexiong
# @File    : test_mcp_session_approval.py

"""通过真实 HTTP、数据库和检查点验证审批配置与会话放行的执行边界。"""

import asyncio
from uuid import uuid4

from fastapi.responses import Response
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import select

from app.api.v1 import ag_ui as endpoint
from app.core.config import get_config
from app.core.enums import (
    ApprovalDecision,
    ApprovalState,
    McpTransport,
    RecoveryPolicy,
    RunCallFailure,
    RunEventType,
    RunState,
    SessionApprovalScope,
    SessionApprovalMode,
    ToolApprovalMode,
    ToolEffect,
    ToolEffectSource,
    ToolInvocationState,
    ToolKind,
    Visibility,
)
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.mcp_connection import McpConnection
from app.models.tool import AgentTool, ToolInvocation, UserTool, UserToolVersion
from app.services.run_executor import execute_text_run
from app.services.run_manager import decide_approval
from app.services.session_approval import allowed_session_tools
from app.services.worker_tools import make_worker_tool_resolver
from tests.test_ag_ui import environment

URL = "/api/v1/agents/1/ag-ui"
POLICY = "/api/v1/agents/1/sessions/session/approval-policy"


class ConversationModel(FakeMessagesListChatModel):
    """固定模型响应，真实经过工具审批中间件和检查点恢复。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保留正式工具绑定调用，避免访问模型供应商。"""
        return self


async def add_tools(sessions, mode=ToolApprovalMode.ALWAYS, owner_id=1):
    """为会话关联两个已验证、无幂等字段的 MCP 写工具。"""
    async with sessions.begin() as db:
        db.add(
            McpConnection(
                id=1,
                owner_id=owner_id,
                name="browser",
                config={
                    "transport": McpTransport.STREAMABLE_HTTP,
                    "url": "https://mcp.invalid/mcp",
                },
                approval_mode=mode,
                version=1,
                enabled=True,
            )
        )
        for tool_id in (1, 2):
            db.add(
                UserTool(
                    id=tool_id,
                    owner_id=owner_id,
                    name=f"click_{tool_id}",
                    kind=ToolKind.MCP,
                    description="浏览器点击",
                    visibility=Visibility.PUBLIC,
                    enabled=True,
                    current_version=1,
                )
            )
            db.add(
                UserToolVersion(
                    tool_id=tool_id,
                    version=1,
                    content_hash=str(tool_id) * 64,
                    input_schema={
                        "type": "object",
                        "properties": {},
                    },
                    effect=ToolEffect.WRITE,
                    effect_source=ToolEffectSource.UNKNOWN,
                    recovery_policy=RecoveryPolicy.MANUAL,
                    requires_approval=True,
                    approval_mode=ToolApprovalMode.INHERIT,
                    verified=True,
                    mcp_connection_id=1,
                    mcp_connection_version=1,
                    mcp_url="https://mcp.invalid/mcp",
                    mcp_tool_name=f"click_{tool_id}",
                )
            )
            db.add(AgentTool(agent_id=1, tool_id=tool_id, tool_version=1))


async def submit(client, headers, version=1):
    """从正式 AG-UI 入口创建运行，以返回的业务 ID 驱动本地 Worker。"""
    response = await client.post(
        URL,
        headers=headers,
        json={
            "threadId": "session",
            "runId": str(uuid4()),
            "messages": [
                {
                    "id": str(uuid4()),
                    "role": "user",
                    "content": "请执行浏览器操作",
                }
            ],
            "forwardedProps": {
                "expected_session_version": version,
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.headers["X-Y-Point-Run-ID"]


def call(tool_id, identity):
    """生成无参数的固定工具调用，使用不同调用 ID 验证后续授权。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": f"user_tool_{tool_id}_click_{tool_id}",
                "args": {},
                "id": identity,
            }
        ],
    )


def local_stream(monkeypatch, calls, lost=False):
    """隔离外部服务与 SSE 等待，实际工具事务及运行图仍由项目实现执行。"""

    async def stream(*args, **kwargs):
        """让 HTTP 提交立即返回，Worker 在测试中显式推进。"""
        return Response(media_type="text/event-stream")

    async def invoke(url, name, arguments, token, timeout, expected_schema, **kwargs):
        """仅在真实执行到远端调用时计数，可模拟副作用后响应丢失。"""
        calls.append(name)
        if lost:
            raise ConnectionError("response lost after external operation")
        return "操作完成"

    monkeypatch.setattr(endpoint, "build_run_stream", stream)
    monkeypatch.setattr("app.services.worker_tools.invoke_mcp_tool", invoke)
    monkeypatch.setattr(get_config(), "MCP_FORCE_APPROVAL", False)
    monkeypatch.setattr(get_config(), "MCP_SESSION_ALLOW_ALL", True)


@pytest.mark.parametrize("lost", [False, True])
def test_never_manual_write_executes_without_approval_and_keeps_unknown(tmp_path, monkeypatch, lost):
    """继承 never 的普通写直接执行；响应丢失仍记录未知且不会补做。"""
    calls = []
    local_stream(monkeypatch, calls, lost)

    async def exercise():
        """通过正式提交和 Worker 执行核对配置、执行状态及副作用次数。"""
        async with environment(tmp_path) as (sessions, client, headers):
            await add_tools(sessions, ToolApprovalMode.NEVER)
            run_id = await submit(client, headers)
            model = ConversationModel(responses=[call(1, "first"), AIMessage(content="操作已处理")])
            result = await execute_text_run(
                sessions,
                run_id,
                model,
                tmp_path / "checkpoints.db",
                resolve_tools=make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=0.05,
            )
            assert calls == ["click_1"]
            assert result.state == (RunState.FAILED if lost else RunState.COMPLETED)
            async with sessions() as db:
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run_id))
                assert invocation.state == (ToolInvocationState.UNKNOWN if lost else ToolInvocationState.COMPLETED)
                if lost:
                    assert invocation.failure_code == RunCallFailure.RESULT_UNKNOWN

    asyncio.run(exercise())


@pytest.mark.parametrize("scope", [SessionApprovalScope.TOOL, SessionApprovalScope.ALL])
def test_scope_http_resume_allows_later_calls_and_revoke_is_versioned(tmp_path, monkeypatch, scope):
    """会话授权与审批原子提交，同轮重复调用免审批，工具范围和撤销仍生效。"""
    calls = []
    local_stream(monkeypatch, calls)

    async def exercise():
        """实际恢复原审批，再验证后续调用、幂等、跨用户和撤销版本。"""
        async with environment(tmp_path) as (sessions, client, headers):
            await add_tools(sessions)
            run_id = await submit(client, headers)
            model = ConversationModel(responses=[call(1, "first"), call(1, "second"), call(2, "third"), AIMessage(content="操作已完成")])
            options = {
                "resolve_tools": make_worker_tool_resolver(sessions),
                "lease_seconds": 10,
                "poll_seconds": 0.05,
            }
            paused = await execute_text_run(sessions, run_id, model, tmp_path / "checkpoints.db", **options)
            assert paused.state == RunState.WAITING_FOR_APPROVAL and not calls
            approval_response = await client.get(f"/api/v1/runs/{run_id}/approval", headers=headers)
            approval = approval_response.json()["data"]
            assert approval["allowed_scopes"] == ["once", "tool", "all"]
            thread = (await client.get(f"{URL}/threads/session", headers=headers)).json()["data"]
            body = {
                "threadId": "session",
                "runId": str(uuid4()),
                "messages": thread["messages"],
                "resume": [
                    {
                        "interruptId": approval["id"],
                        "status": "resolved",
                        "payload": {
                            "decision": "approve",
                            "scope": scope,
                        },
                    }
                ],
            }
            response = await client.post(URL, headers=headers, json=body)
            assert response.status_code == 200, response.text
            assert (await client.post(URL, headers=headers, json=body)).status_code == 200
            before = (await client.get(POLICY, headers=headers)).json()["data"]
            assert before["version"] == 2 and len(before["tools"]) == (1 if scope == SessionApprovalScope.TOOL else 2)
            assert "content_hash" not in str(before) and "mcp.invalid" not in str(before)
            body["resume"][0]["payload"]["scope"] = "once"
            assert (await client.post(URL, headers=headers, json=body)).status_code == 409
            result = await execute_text_run(sessions, run_id, model, tmp_path / "checkpoints.db", resume=True, **options)
            assert calls == (["click_1", "click_1"] if scope == SessionApprovalScope.TOOL else ["click_1", "click_1", "click_2"])
            assert result.state == (RunState.WAITING_FOR_APPROVAL if scope == SessionApprovalScope.TOOL else RunState.COMPLETED), result.failure_code
            async with sessions() as db:
                events = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id))).all()
                assert sum(event.event_type == RunEventType.APPROVAL_POLICY_CHANGED for event in events) == 1
                assert sum(event.event_type == RunEventType.MCP_APPROVAL_ALLOWED for event in events) >= 1
            if scope == SessionApprovalScope.TOOL:
                async with sessions.begin() as db:
                    await decide_approval(db, run_id, 1, ApprovalDecision.APPROVE)
                final = await execute_text_run(sessions, run_id, model, tmp_path / "checkpoints.db", resume=True, **options)
                assert final.state == RunState.COMPLETED
            followup_id = await submit(client, headers, 2)
            followup = ConversationModel(responses=[call(1, "next-turn"), AIMessage(content="后续轮次完成")])
            final = await execute_text_run(sessions, followup_id, followup, tmp_path / "checkpoints.db", **options)
            assert final.state == RunState.COMPLETED and calls[-1] == "click_1"
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "ag-ui-other",
                    "password": "StrongPassword123",
                },
            )
            foreign = {
                "Authorization": f"Bearer {login.json()['data']['access_token']}",
            }
            assert (await client.get(POLICY, headers=foreign)).status_code == 404
            assert (await client.delete(f"{POLICY}?expected_version=2", headers=foreign)).status_code == 404
            assert (await client.delete(f"{POLICY}?expected_version=1", headers=headers)).status_code == 409
            revoke_version = 2
            if scope == SessionApprovalScope.ALL:
                partial = await client.delete(f"{POLICY}?expected_version=2&tool_id=2", headers=headers)
                assert partial.status_code == 200
                assert partial.json()["data"]["version"] == 3
                assert [tool["tool_id"] for tool in partial.json()["data"]["tools"]] == [1]
                async with sessions() as db:
                    remaining = await allowed_session_tools(db, await db.get(AgentRun, run_id))
                    assert set(remaining) == {"user_tool_1_click_1"}
                assert (await client.delete(f"{POLICY}?expected_version=2", headers=headers)).status_code == 409
                revoke_version = 3
            revoked = await client.delete(f"{POLICY}?expected_version={revoke_version}", headers=headers)
            assert revoked.status_code == 200 and revoked.json()["data"]["tools"] == []
            async with sessions() as db:
                assert not await allowed_session_tools(db, await db.get(AgentRun, run_id))

    asyncio.run(exercise())


@pytest.mark.parametrize("constraint", ["deployment", "caller", "shared", "allow_all_disabled", "changed_version"])
def test_session_grants_cannot_override_constraints_or_tool_changes(tmp_path, monkeypatch, constraint):
    """已有放行在强制审批、共享写工具、部署禁用和版本变化后不能继续生效。"""
    calls = []
    local_stream(monkeypatch, calls)

    async def exercise():
        """真实生成审批与会话授权，再改变一项约束并从新数据库会话复核。"""
        async with environment(tmp_path) as (sessions, client, headers):
            await add_tools(sessions)
            run_id = await submit(client, headers)
            model = ConversationModel(responses=[call(1, "first"), AIMessage(content="完成")])
            options = {
                "resolve_tools": make_worker_tool_resolver(sessions),
                "lease_seconds": 10,
                "poll_seconds": 0.05,
            }
            await execute_text_run(sessions, run_id, model, tmp_path / "checkpoints.db", **options)
            async with sessions.begin() as db:
                await decide_approval(db, run_id, 1, ApprovalDecision.APPROVE, scope=SessionApprovalScope.ALL)
            async with sessions() as db:
                assert len(await allowed_session_tools(db, await db.get(AgentRun, run_id))) == 2
            if constraint == "deployment":
                monkeypatch.setattr(get_config(), "MCP_FORCE_APPROVAL", True)
            elif constraint == "allow_all_disabled":
                monkeypatch.setattr(get_config(), "MCP_SESSION_ALLOW_ALL", False)
            else:
                async with sessions.begin() as db:
                    if constraint == "caller":
                        (await db.get(AgentRun, run_id)).require_mcp_approval = True
                    elif constraint == "shared":
                        for tool_id in (1, 2):
                            (await db.get(UserTool, tool_id)).owner_id = 2
                    else:
                        (await db.get(McpConnection, 1)).version += 1
            async with sessions() as db:
                assert not await allowed_session_tools(db, await db.get(AgentRun, run_id))
            assert calls == []

    asyncio.run(exercise())


@pytest.mark.parametrize("constraint", ["deployment", "caller", "shared", "all_disabled", "invalid_scope", "reject_scope"])
def test_http_rejects_unauthorized_session_grants_without_mutating_approval(tmp_path, monkeypatch, constraint):
    """服务端拒绝伪造或受约束的会话放行，失败不改变待审批及会话策略。"""
    calls = []
    local_stream(monkeypatch, calls)

    async def exercise():
        """在生成真实中断后改变约束，再尝试通过 HTTP 扩大审批范围。"""
        async with environment(tmp_path) as (sessions, client, headers):
            await add_tools(sessions)
            run_id = await submit(client, headers)
            model = ConversationModel(responses=[call(1, "first"), AIMessage(content="完成")])
            await execute_text_run(
                sessions,
                run_id,
                model,
                tmp_path / "checkpoints.db",
                resolve_tools=make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=0.05,
            )
            if constraint == "deployment":
                monkeypatch.setattr(get_config(), "MCP_FORCE_APPROVAL", True)
            elif constraint == "all_disabled":
                monkeypatch.setattr(get_config(), "MCP_SESSION_ALLOW_ALL", False)
            elif constraint in ("caller", "shared"):
                async with sessions.begin() as db:
                    if constraint == "caller":
                        (await db.get(AgentRun, run_id)).require_mcp_approval = True
                    else:
                        for tool_id in (1, 2):
                            (await db.get(UserTool, tool_id)).owner_id = 2
            approval = (await client.get(f"/api/v1/runs/{run_id}/approval", headers=headers)).json()["data"]
            if constraint in ("deployment", "caller", "shared"):
                assert approval["allowed_scopes"] == ["once"]
            if constraint == "all_disabled":
                assert approval["allowed_scopes"] == ["once", "tool"]
            thread = (await client.get(f"{URL}/threads/session", headers=headers)).json()["data"]
            response = await client.post(
                URL,
                headers=headers,
                json={
                    "threadId": "session",
                    "runId": str(uuid4()),
                    "messages": thread["messages"],
                    "resume": [
                        {
                            "interruptId": approval["id"],
                            "status": "resolved",
                            "payload": {
                                "decision": "reject_continue" if constraint == "reject_scope" else "approve",
                                "scope": "invalid" if constraint == "invalid_scope" else "all",
                            },
                        }
                    ],
                },
            )
            assert response.status_code == 409, response.text
            current = (await client.get(f"/api/v1/runs/{run_id}/approval", headers=headers)).json()["data"]
            assert current["state"] == ApprovalState.PENDING and current["scope"] == SessionApprovalScope.ONCE
            policy = (await client.get(POLICY, headers=headers)).json()["data"]
            assert policy["mode"] == SessionApprovalMode.ASK and policy["version"] == 1 and not policy["tools"]
            assert not calls

    asyncio.run(exercise())


def test_revocation_after_model_response_prevents_unapproved_side_effect(tmp_path, monkeypatch):
    """模型前读到的放行若在工具执行前撤销，不得据旧内存集合执行副作用。"""
    from app.models.agent_session import AgentSession
    from app.services.session_approval import revoke_session_grants

    calls = []
    local_stream(monkeypatch, calls)

    async def exercise():
        """在模型返回第二次调用时撤销策略，执行中间件必须再次拒绝自动放行。"""
        async with environment(tmp_path) as (sessions, client, headers):
            await add_tools(sessions)
            run_id = await submit(client, headers)
            original = ConversationModel._agenerate

            async def generate(model, *args, **kwargs):
                """在模型响应与工具执行之间插入另一事务的真实授权撤销。"""
                response = await original(model, *args, **kwargs)
                if any(call["id"] == "after-revocation" for call in response.generations[0].message.tool_calls):
                    async with sessions.begin() as db:
                        session = await db.get(AgentSession, "session")
                        await revoke_session_grants(db, session, session.approval_policy_version)
                return response

            monkeypatch.setattr(ConversationModel, "_agenerate", generate)
            model = ConversationModel(responses=[call(1, "first"), call(1, "after-revocation"), AIMessage(content="完成")])
            options = {
                "resolve_tools": make_worker_tool_resolver(sessions),
                "lease_seconds": 10,
                "poll_seconds": 0.05,
            }
            paused = await execute_text_run(sessions, run_id, model, tmp_path / "checkpoints.db", **options)
            assert paused.state == RunState.WAITING_FOR_APPROVAL
            async with sessions.begin() as db:
                await decide_approval(db, run_id, 1, ApprovalDecision.APPROVE, scope=SessionApprovalScope.TOOL)
            result = await execute_text_run(sessions, run_id, model, tmp_path / "checkpoints.db", resume=True, **options)
            assert result.state == RunState.FAILED
            assert calls == ["click_1"], "撤销后仍执行了未经本次明确批准的调用"

    asyncio.run(exercise())
