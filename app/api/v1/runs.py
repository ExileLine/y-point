# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:21:38
# @Author  : yangyuexiong
# @File    : runs.py

"""授权用户提交文本运行并读取持久化结果。"""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.config import get_config
from app.core.security import (
    STREAM_COOKIE_NAME, STREAM_TICKET_SECONDS, get_current_admin, get_stream_admin,
    make_stream_ticket, require_admin, utc_now,
)
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.auth_session import AuthSession
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.agent_access import can_access_run_agent
from app.schemas.run import (
    ApprovalDecisionIn, ApprovalOut, RunCreateIn, RunEventOut, RunMessageOut, RunOut, RunSnapshotOut, WorkerStatusOut,
)
from app.services.message_attachments import load_message_artifacts, load_message_attachments
from app.services.run_manager import (
    TERMINAL_STATES, RunConflictError, RunNotFoundError, create_run, decide_approval, get_owned_approval,
    get_owned_messages, get_owned_run, replay_events, request_cancel,
)
from app.services.worker_monitor import inspect_worker_status

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
            artifact is not None and artifact.owner_id == owner_id and artifact.state == "ready"
            and artifact.status == 1 and artifact.is_deleted == 0
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


@router.post("", summary="创建文本运行")
async def submit_run(body: RunCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """检查模型配置，并在同一事务保存输入与排队意图。"""
    model = await db.get(ModelCatalog, body.model_id)
    provider = await db.get(ProviderConnection, model.provider_id) if model is not None else None
    if model is None or model.status != 1 or model.is_deleted != 0 or provider is None or provider.status != 1 or provider.is_deleted != 0:
        raise CustomException(status_code=404, custom_code=10002, detail="模型不可用")
    if body.enable_text_artifact_tool and "tools" not in (model.verified_capabilities or []):
        raise CustomException(status_code=409, custom_code=10005, detail="模型尚未通过工具调用能力验证")
    options = dict(
        model_id=body.model_id, input_text=body.input_text,
        request_key=body.request_key, thread_id=body.thread_id, model_timeout_seconds=body.model_timeout_seconds,
        tool_timeout_seconds=body.tool_timeout_seconds, max_queue_wait_seconds=body.max_queue_wait_seconds,
        enable_text_artifact_tool=body.enable_text_artifact_tool, max_model_calls=body.max_model_calls,
        publish_answer_artifact=body.publish_answer_artifact,
        max_total_tokens=body.max_total_tokens, max_tool_calls=body.max_tool_calls,
        max_estimated_cost=body.max_estimated_cost,
        cost_pricing=(model.input_price, model.output_price, model.price_currency)
        if body.max_estimated_cost is not None and model.input_price is not None and model.output_price is not None else None,
    )
    try:
        run = await create_run(db, actor.id, **options)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        if body.request_key is None:
            raise CustomException(status_code=409, custom_code=10005, detail="运行创建冲突") from None
        existing = await db.scalar(select(AgentRun.id).where(
            AgentRun.owner_id == actor.id, AgentRun.request_key == body.request_key,
        ))
        if existing is None:
            await db.rollback()
            raise CustomException(status_code=409, custom_code=10005, detail="运行创建冲突") from None
        try:
            run = await create_run(db, actor.id, **options)
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise CustomException(status_code=409, custom_code=10005, detail="运行创建冲突") from None
        except (RunConflictError, ValueError) as exc:
            await db.rollback()
            raise CustomException(status_code=409, custom_code=10005, detail=str(exc)) from None
    except (RunConflictError, ValueError) as exc:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail=str(exc)) from None
    return api_response(http_code=201, code=201, data=_run_data(run), datetime_format=False)


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


@router.post("/{run_id}/approval/decision", summary="决定运行审批请求")
async def submit_approval_decision(
    run_id: str, body: ApprovalDecisionIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session),
):
    """只允许所有者对未过期的待处理请求提交一次同意或拒绝。"""
    await _owned_run(db, run_id, actor.id)
    try:
        approval = await decide_approval(db, run_id, actor.id, body.decision)
        await db.commit()
    except RunNotFoundError:
        raise CustomException(status_code=404, custom_code=10002, detail="审批请求不存在") from None
    except RunConflictError as exc:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail=str(exc)) from None
    return api_response(data=await _approval_data(db, approval, actor.id), datetime_format=False)


@router.get("/{run_id}/events", summary="回放文本运行事件")
async def read_events(
    run_id: str, after: int = Query(default=0, ge=0, description="已收到的最后事件序号"),
    limit: int = Query(default=100, ge=1, le=200, description="本次最多返回事件数"),
    actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session),
):
    """按运行内游标补发已持久化的状态事件。"""
    await _owned_run(db, run_id, actor.id)
    events = await replay_events(db, run_id, actor.id, after=after, limit=limit)
    return api_response(data=[RunEventOut.model_validate(item).model_dump(mode="json") for item in events], is_pop=False)


@router.get("/{run_id}/snapshot", summary="读取运行状态与有界事件快照")
async def read_run_snapshot(
    run_id: str, after: int = Query(default=0, ge=0, description="已收到的最后事件序号"),
    limit: int = Query(default=100, ge=1, le=200, description="本次最多返回事件数"),
    actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session),
):
    """固定本次读取的事件上界，供刷新和断线后逐页追平。"""
    run = await _owned_run(db, run_id, actor.id)
    latest = run.event_seq
    if after > latest:
        raise CustomException(status_code=400, custom_code=10006, detail="事件游标超过当前运行序号")
    events = await replay_events(db, run_id, actor.id, after=after, limit=limit, through=latest)
    next_after = events[-1].sequence if events else after
    snapshot = RunSnapshotOut(
        run=RunOut.model_validate(run), events=[RunEventOut.model_validate(item) for item in events],
        latest_sequence=latest, next_after=next_after, has_more=next_after < latest,
    )
    response = api_response(data=snapshot.model_dump(mode="json"), datetime_format=False)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/{run_id}/stream-ticket", summary="签发浏览器 SSE 订阅凭证")
async def issue_stream_ticket(
    run_id: str, request: Request, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session),
):
    """用 Bearer 换取仅限当前 Run 的短期 HttpOnly Cookie。"""
    await _owned_run(db, run_id, actor.id)
    response = api_response(data={"expires_in": STREAM_TICKET_SECONDS})
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        key=STREAM_COOKIE_NAME, value=make_stream_ticket(actor, request.state.auth_session_id, run_id),
        max_age=STREAM_TICKET_SECONDS, path=f"/api/v1/runs/{run_id}/stream",
        secure=get_config().ENV_NAME not in {"development", "test"}, httponly=True, samesite="strict",
    )
    return response


@router.get("/{run_id}/stream", summary="订阅文本运行状态事件")
async def stream_events(
    run_id: str, request: Request, after: int = Query(default=0, ge=0, description="已收到的最后事件序号"),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID", description="上次收到的运行 ID 与事件序号"),
    actor: Admin = Depends(get_stream_admin), db: AsyncSession = Depends(get_db_session),
):
    """从持久化游标补发状态事件，运行终止后关闭流。"""
    run = await _owned_run(db, run_id, actor.id)
    if last_event_id is not None:
        prefix, separator, value = last_event_id.rpartition(":")
        if separator != ":" or prefix != run_id or not value.isdecimal() or len(value) > 20:
            raise CustomException(status_code=400, custom_code=10006, detail="无效的 Last-Event-ID")
        after = max(after, int(value))
    if after > run.event_seq:
        raise CustomException(status_code=400, custom_code=10006, detail="事件游标超过当前运行序号")
    session_id = request.state.auth_session_id
    token_version = request.state.auth_token_version
    token_expires_at = request.state.auth_token_expires_at
    owner_id = actor.id

    async def event_stream():
        """定期核对授权和数据库事件，并在空闲时发送 SSE 心跳。"""
        cursor = after
        idle = 0
        while not await request.is_disconnected():
            await db.rollback()
            user = await db.get(Admin, owner_id)
            session = await db.get(AuthSession, session_id)
            if (
                user is None or user.status != 1 or user.is_deleted != 0 or user.token_version != token_version
                or time.time() >= token_expires_at or session is None or session.admin_id != owner_id
                or session.revoked_at is not None or session.expires_at <= utc_now()
            ):
                break
            run = await get_owned_run(db, run_id, owner_id)
            if not await can_access_run_agent(db, run.agent_id, owner_id):
                break
            events = await replay_events(db, run_id, owner_id, after=cursor)
            for event in events:
                cursor = event.sequence
                payload = RunEventOut.model_validate(event).model_dump(mode="json")
                yield f"id: {run_id}:{cursor}\nevent: {event.event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if run.state in TERMINAL_STATES and cursor >= run.event_seq:
                if run.state != "completed" or not run.publish_answer_artifact:
                    break
                published = await db.scalar(select(Artifact.id).where(
                    Artifact.run_id == run_id, Artifact.publication_key == "final-answer-v1",
                ).limit(1))
                if published is not None:
                    break
            if events:
                idle = 0
            else:
                idle += 1
                if idle % 10 == 0:
                    yield ": heartbeat\n\n"
            if idle >= 60:
                break
            await asyncio.sleep(1)
        await db.rollback()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
