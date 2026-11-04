# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:23:43
# @Author  : yangyuexiong
# @File    : agent_sessions.py

"""私有 Agent 的会话、运行提交和消息历史接口。"""

from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ArtifactOrigin, ArtifactState, MessageRole, ModelCapability, RunState, SessionModelReason
from app.api.v1.agents import _check_cost_pricing
from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_session import AgentSession
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.agent_access import can_use_agent
from app.schemas.agent_session import (
    SessionCreateIn,
    SessionMessageOut,
    SessionOut,
    SessionPageOut,
    SessionRunCreateIn,
    SessionRunPageOut,
    SessionUpdateIn,
)
from app.schemas.run import RunOut
from app.services.identity import audit
from app.services.attachment_text import CONTEXT_MEDIA_TYPES
from app.services.message_attachments import (
    IMAGE_CONTEXT_MEDIA_TYPES,
    MAX_CONTEXT_ATTACHMENT_BYTES,
    load_message_artifacts,
    load_message_attachments,
)
from app.services.run_manager import RunConflictError, TERMINAL_STATES, create_run
from app.services.run_calls import load_run_calls
from app.services.skills import snapshot_run_skills
from app.services.knowledge import snapshot_run_knowledge
from app.services.user_tools import snapshot_run_tools
from app.services.session_models import effective_session_model, session_model_conflict, session_model_options, validate_session_model


router = APIRouter(prefix="/agents/{agent_id}/sessions", tags=["agent-sessions"])


async def _agent(db: AsyncSession, agent_id: int, owner_id: int) -> AgentConfig:
    """所有者可读历史；指定用户仅在授权有效时使用共享 Agent。"""
    agent = await db.get(AgentConfig, agent_id)
    if agent is None or not await can_use_agent(db, agent_id, owner_id):
        raise CustomException(status_code=404, custom_code=10002, detail="智能体不存在")
    return agent


async def _session(db: AsyncSession, agent_id: int, session_id: str, owner_id: int) -> AgentSession:
    """校验会话属于当前用户和路径中的 Agent。"""
    await _agent(db, agent_id, owner_id)
    session = await db.scalar(
        select(AgentSession).where(
            AgentSession.id == session_id,
            AgentSession.agent_id == agent_id,
            AgentSession.owner_id == owner_id,
            AgentSession.is_deleted == 0,
        )
    )
    if session is None:
        raise CustomException(status_code=404, custom_code=10002, detail="会话不存在")
    return session


async def _session_out(db: AsyncSession, session: AgentSession) -> SessionOut:
    """为迁移前遗留空值返回有效模型，不在只读接口中修改会话。"""
    result = SessionOut.model_validate(session)
    if result.model_id is None:
        agent = await db.get(AgentConfig, session.agent_id)
        result.model_id = await effective_session_model(db, session, agent)
    return result


def _run_response(run: AgentRun):
    """将会话提交的 Run 转为公开结构。"""
    return api_response(http_code=201, code=201, data=RunOut.model_validate(run).model_dump(mode="json"), datetime_format=False)


async def _matching_retry(db: AsyncSession, owner_id: int, session_id: str, body: SessionRunCreateIn) -> AgentRun | None:
    """按用户提交键核对既有 Run，避免配置编辑影响相同请求的幂等重试。"""
    run = await db.scalar(select(AgentRun).where(AgentRun.owner_id == owner_id, AgentRun.request_key == body.request_key))
    if run is None:
        return None
    message = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == MessageRole.USER))
    saved_references = (
        (
            await db.scalars(
                select(AgentMessageAttachment)
                .where(AgentMessageAttachment.message_id == message.id)
                .order_by(AgentMessageAttachment.position)
            )
        ).all()
        if message is not None
        else []
    )
    saved_artifacts = [item.artifact_id for item in saved_references]
    saved_context = {item.artifact_id for item in saved_references if item.included_in_context}
    if (
        run.session_id != session_id
        or body.model_id is not None
        and run.model_id != body.model_id
        or message is None
        or message.content != body.input_text
        or run.publish_answer_artifact != body.publish_answer_artifact
        or saved_artifacts != body.artifact_ids
        or saved_context != set(body.context_artifact_ids)
    ):
        raise CustomException(status_code=409, custom_code=10005, detail="同一提交键的会话、模型、输入、附件或产物设置不一致")
    return run


async def _uploaded_artifacts(db: AsyncSession, owner_id: int, artifact_ids: list[str]) -> list[Artifact]:
    """提交前检查全部引用，避免混入其他用户或已失效文件。"""
    if len(set(artifact_ids)) != len(artifact_ids):
        raise CustomException(status_code=400, custom_code=10006, detail="附件 ID 不能重复")
    if not artifact_ids:
        return []
    items = (
        await db.scalars(
            select(Artifact)
            .where(
                Artifact.id.in_(artifact_ids),
                Artifact.owner_id == owner_id,
                Artifact.source == ArtifactOrigin.UPLOAD,
                Artifact.run_id.is_(None),
                Artifact.state == ArtifactState.READY,
                Artifact.status == 1,
                Artifact.is_deleted == 0,
                (Artifact.expires_at.is_(None) | (Artifact.expires_at > shanghai_now(naive=True))),
            )
            .with_for_update()
        )
    ).all()
    by_id = {item.id: item for item in items}
    if len(by_id) != len(artifact_ids):
        raise CustomException(status_code=404, custom_code=10002, detail="附件不存在或已失效")
    return [by_id[item_id] for item_id in artifact_ids]


async def _ensure_idle(db: AsyncSession, session: AgentSession) -> None:
    """切换、归档或删除前拒绝仍有未结束 Run 的会话。"""
    active = await db.scalar(select(AgentRun.id).where(AgentRun.session_id == session.id, AgentRun.state.not_in(TERMINAL_STATES)).limit(1))
    if active is not None:
        raise session_model_conflict(SessionModelReason.BUSY, "会话仍有未结束的运行，请等待本轮结束后操作")


@router.post("", summary="创建智能体会话")
async def create_session(
    agent_id: int, body: SessionCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """为当前用户的已启用 Agent 创建独立检查点线程。"""
    agent = await db.scalar(
        select(AgentConfig).where(AgentConfig.id == agent_id).with_for_update().execution_options(populate_existing=True)
    )
    if agent is None or not await can_use_agent(db, agent_id, actor.id):
        raise CustomException(status_code=404, custom_code=10002, detail="智能体不存在")
    if agent.is_deleted != 0 or not agent.enabled:
        raise CustomException(status_code=409, custom_code=10005, detail="智能体不可用")
    session = AgentSession(
        id=str(uuid4()),
        owner_id=actor.id,
        agent_id=agent_id,
        thread_id=str(uuid4()),
        model_id=agent.default_model_id,
        title=body.title,
        archived=False,
        version=1,
        is_deleted=0,
        status=1,
    )
    db.add(session)
    audit(db, "session.created", actor_id=actor.id, target_id=agent_id, subject=session.id)
    await db.commit()
    await db.refresh(session)
    return api_response(http_code=201, code=201, data=(await _session_out(db, session)).model_dump(mode="json"), datetime_format=False)


@router.get("", summary="分页查询智能体会话")
async def list_sessions(
    agent_id: int,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    archived: bool | None = Query(None, description="按归档状态筛选"),
    search: str | None = Query(None, max_length=128, description="按标题搜索"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """仅返回当前用户在指定 Agent 下的可见会话。"""
    await _agent(db, agent_id, actor.id)
    conditions = [AgentSession.owner_id == actor.id, AgentSession.agent_id == agent_id, AgentSession.is_deleted == 0]
    if archived is not None:
        conditions.append(AgentSession.archived == archived)
    if search:
        conditions.append(AgentSession.title.contains(search))
    total = await db.scalar(select(func.count()).select_from(AgentSession).where(*conditions))
    items = (
        await db.scalars(
            select(AgentSession)
            .where(*conditions)
            .order_by(AgentSession.create_time.desc(), AgentSession.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    result = SessionPageOut(items=[await _session_out(db, item) for item in items], total=total or 0, page=page, page_size=page_size)
    return api_response(data=result.model_dump(mode="json"), datetime_format=False)


@router.get("/{session_id}", summary="查询智能体会话")
async def get_session(
    agent_id: int, session_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """返回当前用户拥有的会话、当前模型及执行线程 ID。"""
    session = await _session(db, agent_id, session_id, actor.id)
    return api_response(data=(await _session_out(db, session)).model_dump(mode="json"), datetime_format=False)


@router.get("/{session_id}/models", summary="查询会话可切换模型")
async def list_session_models(
    agent_id: int, session_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """向会话所有者展示已验证文本模型和当前配置下的不可选原因。"""
    await _session(db, agent_id, session_id, actor.id)
    agent = await _agent(db, agent_id, actor.id)
    options = await session_model_options(db, agent)
    return api_response(data=[item.model_dump(mode="json") for item in options], is_pop=False)


@router.put("/{session_id}", summary="修改、归档或切换智能体会话模型")
async def update_session(
    agent_id: int,
    session_id: str,
    body: SessionUpdateIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按版本更新空闲会话；切换模型创建新线程，保留同一会话的历史。"""
    session = await _session(db, agent_id, session_id, actor.id)
    # 与提交接口按同一顺序锁定 Agent，避免切换检查后又插入旧线程运行。
    agent = await db.scalar(
        select(AgentConfig).where(AgentConfig.id == agent_id).with_for_update().execution_options(populate_existing=True)
    )
    if agent is None or not await can_use_agent(db, agent_id, actor.id):
        raise CustomException(status_code=404, custom_code=10002, detail="智能体不存在")
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if any(value is None for value in changes.values()):
        raise CustomException(status_code=400, custom_code=10006, detail="会话标题、归档状态和模型不能为空")
    if session.version != body.expected_version:
        raise session_model_conflict(SessionModelReason.VERSION, "会话版本已变化，请刷新后重试")
    if "model_id" in changes:
        await _ensure_idle(db, session)
        if session.archived or changes.get("archived") is True or agent.is_deleted != 0 or not agent.enabled:
            raise session_model_conflict(SessionModelReason.ARCHIVED, "会话已归档或智能体不可用，不能切换模型")
        current_model_id = await effective_session_model(db, session, agent)
        await validate_session_model(db, agent, body.model_id)
        if current_model_id != body.model_id:
            changes["thread_id"] = str(uuid4())
            audit(db, "session.model_changed", actor_id=actor.id, target_id=agent_id, subject=session_id)
    if changes.get("archived") is True:
        await _ensure_idle(db, session)
    if changes:
        result = await db.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.owner_id == actor.id,
                AgentSession.is_deleted == 0,
                AgentSession.version == body.expected_version,
            )
            .values(**changes, version=AgentSession.version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise session_model_conflict(SessionModelReason.VERSION, "会话版本已变化，请刷新后重试")
        audit(db, "session.updated", actor_id=actor.id, target_id=agent_id, subject=session_id)
        await db.commit()
        await db.refresh(session)
    return api_response(data=(await _session_out(db, session)).model_dump(mode="json"), datetime_format=False)


@router.delete("/{session_id}", summary="删除智能体会话")
async def delete_session(
    agent_id: int, session_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """无未结束 Run 时逻辑删除会话，保留历史 Run 以供审计。"""
    session = await _session(db, agent_id, session_id, actor.id)
    await _ensure_idle(db, session)
    result = await db.execute(
        update(AgentSession)
        .where(
            AgentSession.id == session_id,
            AgentSession.owner_id == actor.id,
            AgentSession.is_deleted == 0,
            AgentSession.version == session.version,
        )
        .values(is_deleted=1, archived=True, version=AgentSession.version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="会话版本已变化")
    audit(db, "session.deleted", actor_id=actor.id, target_id=agent_id, subject=session_id)
    await db.commit()
    return api_response(message="会话已删除")


@router.post("/{session_id}/runs", summary="在智能体会话中提交文本运行")
async def submit_session_run(
    agent_id: int,
    session_id: str,
    body: SessionRunCreateIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """同事务保存 Agent 配置快照、用户消息、Run 和调度意图。"""
    owner_id = actor.id
    session = await _session(db, agent_id, session_id, owner_id)
    context_ids = set(body.context_artifact_ids)
    if len(context_ids) != len(body.context_artifact_ids) or not context_ids <= set(body.artifact_ids):
        raise CustomException(status_code=400, custom_code=10006, detail="模型上下文附件必须是未重复的消息附件")
    existing = await _matching_retry(db, owner_id, session_id, body)
    if existing is not None:
        return _run_response(existing)
    agent = await db.scalar(
        select(AgentConfig).where(AgentConfig.id == agent_id).with_for_update().execution_options(populate_existing=True)
    )
    if agent is None or not await can_use_agent(db, agent_id, owner_id):
        raise CustomException(status_code=404, custom_code=10002, detail="智能体不存在")
    if session.archived or agent.is_deleted != 0 or not agent.enabled:
        raise CustomException(status_code=409, custom_code=10005, detail="会话或智能体不可用")
    await _ensure_idle(db, session)
    if body.expected_session_version is not None and session.version != body.expected_session_version:
        raise session_model_conflict(SessionModelReason.VERSION, "会话版本已变化，请刷新后重试")
    model_id = await effective_session_model(db, session, agent)
    if body.model_id is not None and body.model_id != model_id:
        raise session_model_conflict(SessionModelReason.SELECTION, "会话模型已变化，请刷新后重试")
    model = await validate_session_model(db, agent, model_id)
    _check_cost_pricing(model, agent.max_estimated_cost)
    previous_model_id = await db.scalar(
        select(AgentRun.model_id)
        .where(
            AgentRun.session_id == session_id,
            AgentRun.owner_id == owner_id,
            AgentRun.thread_id == session.thread_id,
            AgentRun.state == RunState.COMPLETED,
        )
        .order_by(AgentRun.create_time.desc(), AgentRun.id.desc())
        .limit(1)
    )
    prior_runs = (
        (
            await db.scalars(
                select(AgentRun).where(
                    AgentRun.session_id == session_id, AgentRun.owner_id == owner_id, AgentRun.thread_id == session.thread_id
                )
            )
        ).all()
        if previous_model_id is None
        else []
    )
    reset_thread = bool(prior_runs) and all(item.state in TERMINAL_STATES and item.tool_call_count == 0 for item in prior_runs)
    thread_id = str(uuid4()) if reset_thread else session.thread_id
    artifacts = await _uploaded_artifacts(db, owner_id, body.artifact_ids)
    context_artifacts = [item for item in artifacts if item.id in context_ids]
    image_artifacts = [item for item in context_artifacts if item.media_type in IMAGE_CONTEXT_MEDIA_TYPES]
    if any(item.media_type not in CONTEXT_MEDIA_TYPES | IMAGE_CONTEXT_MEDIA_TYPES for item in context_artifacts):
        raise CustomException(status_code=400, custom_code=10006, detail="附件格式不能进入模型上下文")
    if image_artifacts and ModelCapability.VISION not in (model.verified_capabilities or []):
        raise CustomException(status_code=409, custom_code=10005, detail="会话模型尚未通过视觉能力验证")
    if len(image_artifacts) > 2:
        raise CustomException(status_code=400, custom_code=10006, detail="单轮最多读取两张图片")
    if sum(item.byte_size for item in context_artifacts if item.media_type == "text/plain") > MAX_CONTEXT_ATTACHMENT_BYTES:
        raise CustomException(status_code=413, custom_code=10005, detail="TXT 上下文附件超过 16 KiB 限制")
    options = dict(
        agent_id=agent_id,
        session_id=session_id,
        thread_id=thread_id,
        model_id=model.id,
        input_text=body.input_text,
        request_key=body.request_key,
        publish_answer_artifact=body.publish_answer_artifact,
        agent_config_version=agent.version,
        agent_system_prompt=agent.system_prompt,
        agent_temperature=agent.temperature,
        agent_max_output_tokens=agent.max_output_tokens,
        model_timeout_seconds=agent.model_timeout_seconds,
        tool_timeout_seconds=agent.tool_timeout_seconds,
        max_model_calls=agent.max_model_calls,
        max_tool_calls=agent.max_tool_calls,
        max_total_tokens=agent.max_total_tokens,
        max_estimated_cost=agent.max_estimated_cost,
        cost_pricing=(model.input_price, model.output_price, model.price_currency) if agent.max_estimated_cost is not None else None,
    )
    try:
        claim = await db.execute(
            update(AgentSession)
            .where(
                AgentSession.id == session_id,
                AgentSession.owner_id == owner_id,
                AgentSession.is_deleted == 0,
                AgentSession.archived.is_(False),
                AgentSession.version == session.version,
            )
            .values(version=AgentSession.version + 1, thread_id=thread_id, model_id=model.id)
            .execution_options(synchronize_session=False)
        )
        if claim.rowcount != 1:
            raise RunConflictError("会话已变化")
        run = await create_run(db, owner_id, **options)
        await snapshot_run_skills(db, run.id, agent_id, owner_id)
        await snapshot_run_tools(db, run.id, agent_id, owner_id)
        await snapshot_run_knowledge(db, run.id, agent_id, owner_id)
        if artifacts:
            message_id = await db.scalar(
                select(AgentRunMessage.id).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == MessageRole.USER)
            )
            db.add_all(
                [
                    AgentMessageAttachment(
                        message_id=message_id,
                        artifact_id=item.id,
                        position=position,
                        filename=item.filename,
                        media_type=item.media_type,
                        byte_size=item.byte_size,
                        included_in_context=item.id in context_ids,
                    )
                    for position, item in enumerate(artifacts)
                ]
            )
        await db.commit()
    except (IntegrityError, RunConflictError, ValueError):
        await db.rollback()
        existing = await _matching_retry(db, owner_id, session_id, body)
        if existing is not None:
            return _run_response(existing)
        raise CustomException(status_code=409, custom_code=10005, detail="会话运行提交冲突") from None
    return _run_response(run)


@router.get("/{session_id}/runs", summary="分页查询会话运行")
async def list_session_runs(
    agent_id: int,
    session_id: str,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """供刷新和会话切换后找回已有 Run 及其状态。"""
    await _session(db, agent_id, session_id, actor.id)
    conditions = (AgentRun.owner_id == actor.id, AgentRun.agent_id == agent_id, AgentRun.session_id == session_id)
    total = await db.scalar(select(func.count()).select_from(AgentRun).where(*conditions))
    runs = (
        await db.scalars(
            select(AgentRun)
            .where(*conditions)
            .order_by(AgentRun.create_time.desc(), AgentRun.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    result = SessionRunPageOut(items=[RunOut.model_validate(run) for run in runs], total=total or 0, page=page, page_size=page_size)
    return api_response(data=result.model_dump(mode="json"), datetime_format=False)


@router.get("/{session_id}/messages", summary="按游标读取会话消息")
async def list_session_messages(
    agent_id: int,
    session_id: str,
    after: int = Query(0, ge=0, description="上次读取的消息 ID"),
    limit: int = Query(100, ge=1, le=200, description="本次最多读取的消息数"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按消息 ID 递增回放本会话用户输入与最终回答。"""
    await _session(db, agent_id, session_id, actor.id)
    statement = (
        select(AgentRunMessage)
        .join(AgentRun, AgentRun.id == AgentRunMessage.run_id)
        .where(
            AgentRun.owner_id == actor.id,
            AgentRun.agent_id == agent_id,
            AgentRun.session_id == session_id,
            AgentRunMessage.owner_id == actor.id,
            AgentRunMessage.id > after,
        )
        .order_by(AgentRunMessage.id)
        .limit(limit)
    )
    messages = (await db.scalars(statement)).all()
    by_message = await load_message_attachments(db, actor.id, [item.id for item in messages])
    by_artifact = await load_message_artifacts(db, actor.id, messages)
    calls = await load_run_calls(db, actor.id, list({item.run_id for item in messages}))
    model_rows = await db.execute(
        select(AgentRun.id, AgentRun.model_id, ModelCatalog.display_name, ProviderConnection.name)
        .outerjoin(ModelCatalog, ModelCatalog.id == AgentRun.model_id)
        .outerjoin(ProviderConnection, ProviderConnection.id == ModelCatalog.provider_id)
        .where(AgentRun.id.in_({item.run_id for item in messages}))
    )
    model_labels = {row.id: row for row in model_rows}
    result = []
    for item in messages:
        data = SessionMessageOut.model_validate(item).model_dump(mode="json")
        label = model_labels.get(item.run_id)
        data["model_id"] = label.model_id if label else None
        data["model_name"] = label.display_name if label else None
        data["provider_name"] = label.name if label else None
        data["attachments"] = by_message.get(item.id, [])
        data["artifacts"] = by_artifact.get(item.id, [])
        data["calls"] = [call.model_dump(mode="json") for call in calls.get(item.run_id, [])]
        result.append(data)
    return api_response(data=result, is_pop=False)
