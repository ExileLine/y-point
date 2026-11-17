# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:21:38
# @Author  : yangyuexiong
# @File    : runs.py

"""运行资源、取消与 AG-UI 持久化观察；对话统一通过 AG-UI 提交。"""

import asyncio
import json
import time

from ag_ui.encoder import EventEncoder

from app.services.ag_ui_projection import EVENT_ADAPTER, load_projection

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ArtifactState, RunState
from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.config import get_config
from app.core.security import (
    STREAM_COOKIE_NAME,
    STREAM_TICKET_SECONDS,
    get_current_admin,
    get_stream_admin,
    make_stream_ticket,
    require_admin,
    utc_now,
)
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.auth_session import AuthSession
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.services.agent_access import can_access_run_agent
from app.schemas.run import (
    ApprovalOut,
    RunMessageOut,
    RunOut,
    WorkerStatusOut,
)
from app.services.message_attachments import load_message_artifacts, load_message_attachments
from app.services.run_manager import (
    TERMINAL_STATES,
    RunConflictError,
    RunNotFoundError,
    get_owned_approval,
    get_owned_messages,
    get_owned_run,
    replay_events,
    request_cancel,
)
from app.services.worker_monitor import inspect_worker_status
from app.services.run_calls import load_run_calls

router = APIRouter(prefix="/runs", tags=["runs"])


def _run_data(run) -> dict:
    """将运行转换为不含租约和内部检查点的公开结构。"""
    return RunOut.model_validate(run).model_dump(mode="json")


async def _approval_data(db: AsyncSession, approval: object, owner_id: int) -> dict:
    """仅为当前所有者补充仍可访问的审批目标文件名。"""
    data = ApprovalOut.model_validate(approval).model_dump(mode="json")
    for action in data["actions"]:
        resource_id = action.get("resource_id")
        if resource_id is None:
            continue
        artifact = await db.get(Artifact, resource_id)
        if (
            artifact is not None
            and artifact.owner_id == owner_id
            and artifact.state == ArtifactState.READY
            and artifact.status == 1
            and artifact.is_deleted == 0
            and (artifact.expires_at is None or artifact.expires_at > shanghai_now(naive=True))
        ):
            action["resource_name"] = artifact.filename
    return data


@router.get("/workers/status", summary="查询运行执行器状态")
async def read_worker_status(_: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """向管理员展示新鲜心跳、排队等待和过期运行数量。"""
    status = await inspect_worker_status(db)
    return api_response(data=WorkerStatusOut.model_validate(status).model_dump(mode="json"), datetime_format=False)


async def _owned_run(db: AsyncSession, run_id: str, owner_id: int):
    """将不存在或跨用户运行统一映射为公开的 404 错误。"""
    try:
        run = await get_owned_run(db, run_id, owner_id)
    except RunNotFoundError:
        raise CustomException(status_code=404, custom_code=10002, detail="运行不存在") from None
    if not await can_access_run_agent(db, run.agent_id, owner_id):
        raise CustomException(status_code=404, custom_code=10002, detail="运行不存在")
    return run


@router.get("/{run_id}", summary="查询文本运行详情")
async def read_run(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """只返回当前用户拥有的运行状态。"""
    return api_response(data=_run_data(await _owned_run(db, run_id, actor.id)), datetime_format=False)


@router.post("/{run_id}/cancel", summary="取消文本运行")
async def cancel_run(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """请求取消运行；运行中由执行器停止后确认。"""
    await _owned_run(db, run_id, actor.id)
    try:
        run = await request_cancel(db, run_id, actor.id)
        await db.commit()
    except RunConflictError as exc:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail=str(exc)) from None
    return api_response(data=_run_data(run), datetime_format=False)


@router.get("/{run_id}/messages", summary="查询文本运行消息")
async def read_messages(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """返回用户输入及已落库的最终可见回答。"""
    await _owned_run(db, run_id, actor.id)
    messages = await get_owned_messages(db, run_id, actor.id)
    by_message = await load_message_attachments(db, actor.id, [item.id for item in messages])
    by_artifact = await load_message_artifacts(db, actor.id, messages)
    result = []
    for item in messages:
        data = RunMessageOut.model_validate(item).model_dump(mode="json")
        data["attachments"] = by_message.get(item.id, [])
        data["artifacts"] = by_artifact.get(item.id, [])
        result.append(data)
    return api_response(data=result, is_pop=False)


@router.get("/{run_id}/approval", summary="查询运行审批请求")
async def read_approval(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """仅向运行所有者展示待审核工具和参数摘要。"""
    await _owned_run(db, run_id, actor.id)
    try:
        approval = await get_owned_approval(db, run_id, actor.id)
    except RunNotFoundError:
        raise CustomException(status_code=404, custom_code=10002, detail="审批请求不存在") from None
    return api_response(data=await _approval_data(db, approval, actor.id), datetime_format=False)


@router.get("/{run_id}/calls", summary="读取本轮实际调用名称与执行状态")
async def read_run_calls(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """复核运行和 Agent 当前权限，仅公开调用类型、名称和事件确认的状态。"""
    await _owned_run(db, run_id, actor.id)
    calls = await load_run_calls(db, actor.id, [run_id])
    response = api_response(data=[call.model_dump(mode="json") for call in calls.get(run_id, [])], is_pop=False)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/{run_id}/ag-ui/snapshot", summary="读取 AG-UI 1.0 可恢复事件快照")
async def ag_ui_snapshot(
    run_id: str,
    after: int = Query(0, ge=0, description="AG-UI 线事件游标"),
    limit: int = Query(200, ge=1, le=500, description="每页协议事件数量"),
    through: int | None = Query(None, ge=0, description="首个快照返回的固定源事件上界"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按固定源事件上界回放，页间新增事件由后续实时订阅补齐。"""
    run = await _owned_run(db, run_id, actor.id)
    source_high_water = run.event_seq if through is None else through
    if source_high_water > run.event_seq:
        raise CustomException(status_code=400, custom_code=10006, detail="快照上界超过最新源事件")
    # 不修改 ORM 实例，只把冻结的事件上界传递给投影器。
    projection, frames = await load_projection(db, run, through=source_high_water)
    if after > projection.sequence:
        raise CustomException(status_code=400, custom_code=10006, detail="AG-UI 游标超过快照事件数")
    selected = frames[after : after + limit]
    next_after = selected[-1]["sequence"] if selected else after
    resources_complete = not run.publish_answer_artifact or bool(
        await db.scalar(select(Artifact.id).where(Artifact.run_id == run_id, Artifact.publication_key == "final-answer-v1").limit(1)),
    )
    response = api_response(
        data={
            "run": _run_data(run),
            "resources_complete": resources_complete,
            "events": selected,
            "next_after": next_after,
            "latest_sequence": projection.sequence,
            "through": source_high_water,
            "has_more": next_after < projection.sequence,
        },
        datetime_format=False,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/{run_id}/stream-ticket", summary="签发浏览器 SSE 订阅凭证")
async def issue_stream_ticket(run_id: str, request: Request, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """用 Bearer 换取仅限当前 Run 的短期 HttpOnly Cookie。"""
    await _owned_run(db, run_id, actor.id)
    response = api_response(
        data={
            "expires_in": STREAM_TICKET_SECONDS,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=STREAM_COOKIE_NAME,
        value=make_stream_ticket(actor, request.state.auth_session_id, run_id),
        max_age=STREAM_TICKET_SECONDS,
        path=f"/api/v1/runs/{run_id}/stream",
        secure=get_config().stream_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/{run_id}/stream", summary="订阅文本运行状态事件")
async def stream_events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0, description="已收到的最后事件序号"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID", description="上次收到的运行 ID 与事件序号"),
    actor: Admin = Depends(get_stream_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """从持久化游标补发标准事件；订阅断开不取消 Worker。"""
    return await build_run_stream(run_id, request, actor, db, after=after, last_event_id=last_event_id)


async def build_run_stream(
    run_id: str,
    request: Request,
    actor: Admin,
    db: AsyncSession,
    *,
    after: int = 0,
    last_event_id: str | None = None,
    stop_at_interrupt: bool = False,
):
    """共享标准 POST 与持久化 GET 的鉴权流实现，保留持续撤权检查。"""
    run = await _owned_run(db, run_id, actor.id)
    if last_event_id is not None:
        prefix, separator, value = last_event_id.rpartition(":")
        if separator != ":" or prefix != run_id or not value.isdecimal() or len(value) > 20:
            raise CustomException(status_code=400, custom_code=10006, detail="无效的 Last-Event-ID")
        after = max(after, int(value))
    projection, initial_frames = await load_projection(db, run)
    if after > projection.sequence:
        raise CustomException(status_code=400, custom_code=10006, detail="AG-UI 游标超过当前运行事件数")
    session_id = request.state.auth_session_id
    token_version = request.state.auth_token_version
    token_expires_at = request.state.auth_token_expires_at
    owner_id = actor.id

    async def authorized_run():
        """重新打开只读事务，避免长回放期间继续使用已撤销的身份或共享授权。"""
        await db.rollback()
        user = await db.get(Admin, owner_id)
        session = await db.get(AuthSession, session_id)
        if (
            user is None
            or user.status != 1
            or user.is_deleted != 0
            or user.token_version != token_version
            or time.time() >= token_expires_at
            or session is None
            or session.admin_id != owner_id
            or session.revoked_at is not None
            or session.expires_at <= utc_now()
        ):
            return None
        current = await get_owned_run(db, run_id, owner_id)
        return current if await can_access_run_agent(db, current.agent_id, owner_id) else None

    async def event_stream():
        """回放与实时事件逐帧核对授权；断开观察连接不会取消后台任务。"""
        encoder = EventEncoder()
        cursor = projection.source_sequence
        pending_frames = initial_frames
        last_activity = last_heartbeat = time.monotonic()
        while not await request.is_disconnected():
            run = await authorized_run()
            if run is None:
                break
            # 先投影为独立结构，再释放 ORM 事务，防止授权刷新使源事件对象过期。
            outgoing = [("protocol", frame) for frame in pending_frames if frame["sequence"] > after]
            pending_frames = []
            events = await replay_events(db, run_id, owner_id, after=cursor)
            for event in events:
                cursor = event.sequence
                if (
                    not projection.open
                    and not stop_at_interrupt
                    and event.event_type == "state"
                    and event.state == RunState.CANCELLED
                    and not (event.details or {}).get("protocol_run_id")
                ):
                    outgoing.append(("y-point.run-state", _run_data(run)))
                if not projection.open and event.event_type == "artifact_published":
                    outgoing.append(
                        (
                            "y-point.resource",
                            {
                                "version": 1,
                                "artifactId": event.artifact_id,
                            },
                        )
                    )
                outgoing.extend(("protocol", frame) for frame in projection.feed(event) if frame["sequence"] > after)
            for name, data in outgoing:
                run = await authorized_run()
                if run is None or await request.is_disconnected():
                    return
                if name == "protocol":
                    yield f"id: {run_id}:{data['sequence']}\n" + encoder.encode(EVENT_ADAPTER.validate_python(data["event"]))
                    if stop_at_interrupt and data["event"]["type"] in ("RUN_FINISHED", "RUN_ERROR"):
                        return
                else:
                    yield f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            if run.state in TERMINAL_STATES and cursor >= run.event_seq:
                if run.state != RunState.COMPLETED or not run.publish_answer_artifact:
                    break
                published = await db.scalar(select(Artifact.id).where(Artifact.run_id == run_id, Artifact.publication_key == "final-answer-v1").limit(1))
                if published is not None:
                    break
            now = time.monotonic()
            if events:
                last_activity = now
            elif now - last_heartbeat >= 10:
                last_heartbeat = now
                yield ": heartbeat\n\n"
            if now - last_activity >= 60:
                break
            await db.rollback()
            await asyncio.sleep(0 if events else 0.2)
        await db.rollback()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
