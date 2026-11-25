# -*- coding: utf-8 -*-
# @Time    : 2026-11-17 10:08:18
# @Author  : yangyuexiong
# @File    : ag_ui.py

"""标准 AG-UI HTTP/SSE 入口，复用平台会话授权、幂等提交与 Worker。"""

from ag_ui.core import AgentCapabilities, RunAgentInput
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent_sessions import _session, create_session_run, load_session_message_details
from app.api.v1.runs import _owned_run, build_run_stream
from app.core.custom_exception import CustomException
from app.core.enums import SessionApprovalScope, AgUiConflictReason, ApprovalState, MessageRole, SessionSubmissionReason
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.run_approval import RunApproval
from app.schemas.agent_session import SessionRunCreateIn
from app.services.ag_ui_projection import load_projection
from app.services.ag_ui_history import public_history, public_message_id_exists
from app.services.run_manager import RunConflictError, decide_approval
from app.services.session_approval import session_policy_output

router = APIRouter(prefix="/agents/{agent_id}/ag-ui", tags=["ag-ui"])


def protocol_conflict(message: str, *, reason: AgUiConflictReason | SessionSubmissionReason = AgUiConflictReason.PROTOCOL_INPUT) -> CustomException:
    """在 SSE 开始前返回稳定原因码，客户端无需匹配文案。"""
    return CustomException(
        status_code=409,
        custom_code=10005,
        detail=message,
        data={
            "reason": reason,
        },
    )


@router.get("/capabilities", summary="读取 AG-UI 支持范围")
async def capabilities(agent_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """准确声明当前标准子集与版本化恢复扩展，不授予客户端工具执行权限。"""
    from app.services.agent_access import can_use_agent

    if not await can_use_agent(db, agent_id, actor.id):
        raise CustomException(status_code=404, custom_code=10002, detail="智能体不存在")
    data = AgentCapabilities.model_validate(
        {
            "protocolVersion": "1.0",
            "humanInTheLoop": {
                "supported": True,
                "approvals": True,
                "interrupts": True,
                "approveWithEdits": False,
            },
            "transport": {
                "streaming": True,
                "websocket": False,
                "resumable": False,
            },
            "reasoning": {
                "supported": True,
                "streaming": True,
                "encrypted": False,
            },
            "tools": {
                "supported": True,
                "parallelCalls": True,
                "clientProvided": False,
            },
            "custom": {
                "yPoint": {
                    "version": 1,
                    "history": "server-public-history-exact-match",
                    "historyModes": ["server-public-history-exact-match", "server-authoritative-version-check"],
                    "versionCheck": {
                        "field": "forwardedProps.expected_session_version",
                        "messages": "last-user-message",
                        "nullUsesLegacy": True,
                        "resume": False,
                    },
                    "tools": "server-authorized-display-only",
                    "recovery": "durable-snapshot-and-wire-cursor",
                    "approvalScopes": list(SessionApprovalScope),
                    "clientTools": False,
                    "clientState": False,
                    "clientContext": False,
                },
            },
        },
    )
    return JSONResponse(data.model_dump(mode="json", by_alias=True, exclude_none=True))


@router.get("/threads/{session_id}", summary="读取标准入口所需的公开会话历史")
async def thread_history(agent_id: int, session_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """让通用协议客户端使用准确的服务器公开历史与会话版本。"""
    session = await _session(db, agent_id, session_id, actor.id)
    response = api_response(
        data={
            "threadId": session.id,
            "version": session.version,
            "approvalPolicy": session_policy_output(session),
            "messages": await public_history(db, session.id, actor.id),
            "messageDetails": await load_session_message_details(db, agent_id, session.id, actor.id),
        },
    )
    await db.commit()  # 仅持久化派生缓存；历史读取之前已完成授权检查。
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("", summary="以 AG-UI RunAgentInput 提交或恢复运行并订阅 SSE")
async def run_agent(
    agent_id: int,
    body: RunAgentInput,
    request: Request,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按可选会话版本或旧完整历史校验提交，继续由服务器管理执行上下文。"""
    if body.protocol_version not in (None, "1.0"):
        raise protocol_conflict("当前只支持 AG-UI 1.0")
    if not 1 <= len(body.run_id) <= 96 or body.parent_run_id or body.tools or body.context or body.state not in (None, {}):
        raise protocol_conflict("当前入口不接受子运行、客户端工具、上下文或非空状态")
    await _session(db, agent_id, body.thread_id, actor.id)
    if body.resume:
        return await resume_agent(agent_id, body, request, actor, db)
    if not body.messages or body.messages[-1].role != MessageRole.USER or not isinstance(body.messages[-1].content, str):
        raise protocol_conflict("消息必须以一条纯文本用户消息结尾")
    request_key = f"ag-ui:{body.run_id}"
    existing = await db.scalar(select(AgentRun).where(AgentRun.owner_id == actor.id, AgentRun.request_key == request_key))
    if existing is not None and (existing.session_id != body.thread_id or existing.agent_id != agent_id):
        raise protocol_conflict("runId 已用于另一会话", reason=SessionSubmissionReason.REQUEST_CONFLICT)
    props = body.forwarded_props or {}
    if not isinstance(props, dict) or set(props) - {
        "model_id",
        "expected_session_version",
        "artifact_ids",
        "context_artifact_ids",
        "publish_answer_artifact",
        "require_mcp_approval",
    }:
        raise protocol_conflict("forwardedProps 仅接受预期模型、会话版本、附件、回答导出和 MCP 审批收紧选项")
    from pydantic import ValidationError

    try:
        draft = SessionRunCreateIn(input_text=body.messages[-1].content, request_key=request_key, **props)
    except ValidationError:
        raise protocol_conflict("会话输入或附件选项无效") from None
    message_id = body.messages[-1].id
    if not 1 <= len(message_id) <= 128:
        raise protocol_conflict("用户消息 ID 为空或过长", reason=AgUiConflictReason.MESSAGE_ID_CONFLICT)
    if draft.expected_session_version is None:
        retry_user = None
        if existing is not None:
            retry_user = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == existing.id, AgentRunMessage.role == MessageRole.USER))
        expected = await public_history(db, body.thread_id, actor.id, through_message_id=retry_user.id - 1 if retry_user else None)
        supplied = [message.model_dump(mode="json", by_alias=True, exclude_none=True) for message in body.messages[:-1]]
        if supplied != expected:
            raise protocol_conflict("消息历史与服务器公开历史不一致，请读取 threads 快照后重试", reason=AgUiConflictReason.HISTORY_MISMATCH)
        duplicate_id = message_id in {message["id"] for message in expected}
    else:
        # 附带的客户端旧消息不参与校验或执行；只读服务器会话和本次用户输入。
        duplicate_id = False if existing is not None else await public_message_id_exists(db, body.thread_id, actor.id, message_id)
    if duplicate_id:
        raise protocol_conflict("用户消息 ID 与历史重复", reason=AgUiConflictReason.MESSAGE_ID_CONFLICT)
    if existing is not None:
        user_message = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == existing.id, AgentRunMessage.role == MessageRole.USER))
        if user_message.ag_ui_message_id != body.messages[-1].id:
            raise protocol_conflict("幂等重试的用户消息 ID 已变化", reason=SessionSubmissionReason.REQUEST_CONFLICT)
    run = await create_session_run(
        agent_id,
        body.thread_id,
        draft,
        actor,
        db,
        protocol_run_id=body.run_id,
        protocol_user_message_id=body.messages[-1].id,
    )
    business_run_id = run.id
    response = await build_run_stream(business_run_id, request, actor, db, stop_at_interrupt=True)
    response.headers["X-Y-Point-Run-ID"] = business_run_id
    response.headers["X-Y-Point-After"] = "0"
    return response


async def resume_agent(agent_id: int, body: RunAgentInput, request: Request, actor: Admin, db: AsyncSession):
    """以新协议运行 ID 回答唯一待审批中断，同时继续原业务预算和审计。"""
    if len(body.resume) != 1 or body.forwarded_props:
        raise protocol_conflict("审批恢复仅接受一条 resume，且不接受 forwardedProps")
    entry = body.resume[0]
    approval = await db.get(RunApproval, entry.interrupt_id)
    if approval is None or approval.owner_id != actor.id:
        raise CustomException(status_code=404, custom_code=10002, detail="审批不存在")
    run = await _owned_run(db, approval.run_id, actor.id)
    if run.agent_id != agent_id or run.session_id != body.thread_id:
        raise protocol_conflict("中断不属于当前会话")
    supplied = [message.model_dump(mode="json", by_alias=True, exclude_none=True) for message in body.messages]
    resume_user = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == MessageRole.USER))
    pause_events = (
        await db.scalars(
            select(AgentRunEvent).where(AgentRunEvent.run_id == run.id, AgentRunEvent.state == "waiting_for_approval").order_by(AgentRunEvent.sequence),
        )
    ).all()
    pause_event = next((event for event in pause_events if (event.details or {}).get("interrupt_id") == approval.id), None)
    if pause_event is None:
        raise protocol_conflict("审批缺少对应的公开中断事件")
    if supplied != await public_history(
        db,
        body.thread_id,
        actor.id,
        through_message_id=resume_user.id,
        event_through=(run.id, pause_event.sequence),
    ):
        raise protocol_conflict("恢复历史与服务器公开历史不一致", reason=AgUiConflictReason.HISTORY_MISMATCH)
    payload = entry.payload or {}
    if not isinstance(payload, dict) or set(payload) - {"decision", "decisions", "scope"}:
        raise protocol_conflict("审批 payload 只接受 decision、逐项 decisions 与会话 scope")
    decision = "reject" if entry.status == "cancelled" else payload.get("decision")
    if decision not in ("approve", "reject", "reject_continue", "stop", "resolve"):
        raise protocol_conflict("恢复时必须明确审批决定")
    try:
        scope = SessionApprovalScope(payload.get("scope", SessionApprovalScope.ONCE))
    except (ValueError, TypeError):
        raise protocol_conflict("无效的会话放行范围") from None
    projection, frames = await load_projection(db, run)
    if (
        body.run_id == run.ag_ui_run_id
        or any(frame["event"]["type"] == "RUN_STARTED" and frame["event"]["runId"] == body.run_id for frame in frames)
        and approval.ag_ui_run_id != body.run_id
    ):
        raise protocol_conflict("恢复必须使用新的 runId")
    cursor = next(
        (
            frame["sequence"]
            for frame in frames
            if frame["event"]["type"] == "RUN_FINISHED"
            and frame["event"].get("outcome", {}).get("type") == "interrupt"
            and frame["event"]["outcome"]["interrupts"][0]["id"] == approval.id
        ),
        None,
    )
    if cursor is None:
        raise protocol_conflict("审批中断尚未持久化")
    pending = approval.state == ApprovalState.PENDING
    if pending or approval.ag_ui_run_id == body.run_id:
        try:
            await decide_approval(db, run.id, actor.id, decision, expected_approval_id=approval.id, decisions=payload.get("decisions"), scope=scope)
        except (RunConflictError, ValueError):
            raise protocol_conflict("审批已变化或逐项决定无效，请刷新后重试") from None
        approval.ag_ui_run_id = body.run_id
        if pending and run.state == "cancelled":
            state_event = await db.scalar(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id, AgentRunEvent.sequence == run.event_seq))
            state_event.details = {
                "protocol_run_id": body.run_id,
            }
        await db.commit()
    else:
        raise protocol_conflict("审批已由另一请求决定")
    business_run_id = run.id
    response = await build_run_stream(business_run_id, request, actor, db, after=cursor, stop_at_interrupt=True)
    response.headers["X-Y-Point-Run-ID"] = business_run_id
    response.headers["X-Y-Point-After"] = str(cursor)
    return response
