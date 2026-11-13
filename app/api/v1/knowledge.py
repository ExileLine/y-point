# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:55:04
# @Author  : yangyuexiong
# @File    : knowledge.py

"""知识库、文档、持久化索引任务、检索及引用接口。"""

import asyncio
import time
from contextlib import suppress

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import KnowledgeIndexState, KnowledgeJobState, ModelCapability, ToolLeaseKind, KnowledgePdfMode, ArtifactState, KnowledgeSourceKind
from app.core.response import api_response
from app.core.security import get_current_admin, require_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.knowledge import (
    KnowledgeBase,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIndex,
    KnowledgeJob,
    KnowledgeRetrieval,
    KnowledgeShare,
)
from app.schemas.knowledge import KnowledgeCreateIn, KnowledgeRevisionIn, KnowledgeSearchIn, KnowledgeShareIn, KnowledgeUpdateIn
from app.services.artifact_store import ArtifactValidationError, artifact_read_max_bytes, artifact_root, enforce_owner_quota, save_upload
from app.services.embedding_adapter import embed_texts, embedding_identity, load_embedding_model
from app.services.identity import audit
from app.services.knowledge import (
    TERMINAL_KNOWLEDGE_JOBS,
    cancel_knowledge_runs,
    enqueue_knowledge_index,
    knowledge_access,
    load_document_processing_preview,
    read_knowledge_citation,
    remove_knowledge_document,
    require_knowledge_base,
    search_knowledge,
    validate_knowledge_configuration,
)
from app.services.knowledge_parser import KnowledgeProcessingError, render_pdf_page_isolated
from app.schemas.knowledge import KnowledgeDocumentOptions
from pydantic import ValidationError
from app.services.knowledge_formats import ROUTED_MEDIA_TYPES
from app.services.private_blob_store import delete_blob, read_blob
from app.services.tool_execution import ToolCapacityError, tool_execution_slot

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])
embedding_router = APIRouter(prefix="/models", tags=["models"])
retrieval_router = APIRouter(prefix="/runs", tags=["knowledge"])
SUPPORTED_KNOWLEDGE_TYPES = ROUTED_MEDIA_TYPES | frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
)


async def _owned_job(db: AsyncSession, job_id: int, owner_id: int) -> KnowledgeJob:
    """任务进度和管理只允许所属知识库创建者，未授权统一 404。"""
    job = await db.get(KnowledgeJob, job_id)
    if job is None or job.owner_id != owner_id:
        raise CustomException(status_code=404, custom_code=10002, detail="知识任务不存在")
    return job


async def _cancel_index_jobs(db: AsyncSession, knowledge_base_id: int) -> None:
    """标记资源的未终结任务取消，实际清理由任务收尾执行。"""
    indices = select(KnowledgeIndex.id).where(KnowledgeIndex.knowledge_base_id == knowledge_base_id)
    await db.execute(
        update(KnowledgeJob)
        .where(KnowledgeJob.index_id.in_(indices), KnowledgeJob.state.not_in(TERMINAL_KNOWLEDGE_JOBS))
        .values(state=KnowledgeJobState.CANCEL_REQUESTED),
    )


@router.post("", summary="创建私有知识库")
async def create_knowledge_base(body: KnowledgeCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """校验分块和嵌入配置，创建默认私有且尚无索引的知识库。"""
    await validate_knowledge_configuration(db, body.model_dump())
    item = KnowledgeBase(owner_id=actor.id, **body.model_dump())
    db.add(item)
    await db.flush()
    audit(db, "knowledge.created", actor_id=actor.id, target_id=item.id)
    await db.commit()
    return api_response(http_code=201, code=201, data=item.to_dict(), datetime_format=False)


@router.get("", summary="列出本人和指定共享的知识库")
async def list_knowledge_bases(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """分页返回实际用户有权访问的知识库，不通过 Agent 共享推导权限。"""
    from sqlalchemy import func

    condition = knowledge_access(actor.id)
    items = (await db.scalars(select(KnowledgeBase).where(condition).order_by(KnowledgeBase.id.desc()).offset((page - 1) * page_size).limit(page_size))).all()
    total = await db.scalar(select(func.count()).select_from(KnowledgeBase).where(condition))
    return api_response(
        data={
            "items": [item.to_dict() for item in items],
            "total": total,
            "page": page,
            "page_size": page_size,
        },
        datetime_format=False,
    )


@router.get("/jobs/{job_id}", summary="查询知识索引处理阶段")
async def get_knowledge_job(job_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """返回脱敏阶段、尝试次数及嵌入用量，不返回租约令牌。"""
    job = await _owned_job(db, job_id, actor.id)
    return api_response(data=job.to_dict(exclude={"lease_token"}), datetime_format=False)


@router.post("/jobs/{job_id}/cancel", summary="取消知识索引任务")
async def cancel_knowledge_job(job_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """幂等请求取消任务，保留既有完整活动索引。"""
    await _owned_job(db, job_id, actor.id)
    await db.execute(
        update(KnowledgeJob)
        .where(KnowledgeJob.id == job_id, KnowledgeJob.state.not_in(TERMINAL_KNOWLEDGE_JOBS))
        .values(state=KnowledgeJobState.CANCEL_REQUESTED),
    )
    await db.commit()
    return api_response(message="知识索引任务已请求取消")


@router.post("/jobs/{job_id}/retry", summary="重试失败或取消的知识索引任务")
async def retry_knowledge_job(
    job_id: int,
    expected_attempts: int = Query(ge=1),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """仅重试仍是最新期望代的任务，过期配置必须创建新代，最多三次尝试。"""
    from app.services.knowledge_jobs import KNOWLEDGE_MAX_ATTEMPTS

    job = await _owned_job(db, job_id, actor.id)
    index = await db.get(KnowledgeIndex, job.index_id)
    kb = await require_knowledge_base(db, index.knowledge_base_id, actor.id, manage=True)
    if index.revision != kb.revision or job.attempts >= KNOWLEDGE_MAX_ATTEMPTS:
        raise CustomException(status_code=409, custom_code=10005, detail="任务已过期或达到重试上限，请重建索引")
    result = await db.execute(
        update(KnowledgeJob)
        .where(
            KnowledgeJob.id == job_id,
            KnowledgeJob.attempts == expected_attempts,
            KnowledgeJob.state.in_([KnowledgeJobState.FAILED, KnowledgeJobState.CANCELLED]),
            KnowledgeJob.lease_token.is_(None),
        )
        .values(state=KnowledgeJobState.UPLOADED, failure_code=None),
    )
    if result.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="任务状态已变化或当前不可重试")
    index.state = KnowledgeIndexState.BUILDING
    await db.commit()
    return api_response(message="知识索引任务已重新排队")


@router.get("/{knowledge_base_id}", summary="查询知识库详情")
async def get_knowledge_base(knowledge_base_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """返回当前配置及活动代，构建中配置不覆盖可用旧代。"""
    item = await require_knowledge_base(db, knowledge_base_id, actor.id)
    return api_response(data=item.to_dict(), datetime_format=False)


@router.put("/{knowledge_base_id}", summary="更新知识库配置并重建新索引代")
async def update_knowledge_base(
    knowledge_base_id: int,
    body: KnowledgeUpdateIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """完整替换配置，模型或维度改变时必须成功构建新代后切换。"""
    item = await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True, lock=True)
    values = body.model_dump(exclude={"expected_revision"})
    await validate_knowledge_configuration(db, values)
    for key, value in values.items():
        setattr(item, key, value)
    job = await enqueue_knowledge_index(db, item, body.expected_revision)
    audit(db, "knowledge.updated", actor_id=actor.id, target_id=item.id)
    await db.commit()
    return api_response(
        data={
            "knowledge_base": item.to_dict(),
            "job_id": job.id,
        },
        datetime_format=False,
    )


@router.delete("/{knowledge_base_id}", summary="删除知识库并立即阻断所有检索")
async def delete_knowledge_base(
    knowledge_base_id: int,
    expected_revision: int = Query(ge=1),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """逻辑删除先于后台文件清理，并取消任务和受影响运行。"""
    item = await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True, lock=True)
    changed = await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id == item.id, KnowledgeBase.revision == expected_revision)
        .values(is_deleted=1, revision=expected_revision + 1),
    )
    if changed.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="知识库版本已变化")
    await _cancel_index_jobs(db, item.id)
    await cancel_knowledge_runs(db, item.id)
    audit(db, "knowledge.deleted", actor_id=actor.id, target_id=item.id)
    await db.commit()
    return api_response(message="知识库已删除，文件等待后台清理")


@router.post("/{knowledge_base_id}/shares", summary="向指定用户只读共享知识库")
async def share_knowledge_base(
    knowledge_base_id: int,
    body: KnowledgeShareIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """共享仅授予知识查询，不授予文档或配置管理权限。"""
    await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True, lock=True)
    user = await db.get(Admin, body.user_id)
    if user is None or user.is_deleted != 0 or user.status != 1 or user.id == actor.id:
        raise CustomException(status_code=400, custom_code=10006, detail="共享用户不可用")
    existing = await db.scalar(select(KnowledgeShare).where(KnowledgeShare.knowledge_base_id == knowledge_base_id, KnowledgeShare.user_id == body.user_id))
    if existing is None:
        db.add(KnowledgeShare(knowledge_base_id=knowledge_base_id, user_id=body.user_id))
    audit(db, "knowledge.shared", actor_id=actor.id, target_id=knowledge_base_id)
    await db.commit()
    return api_response(message="知识库已向指定用户只读共享")


@router.get("/{knowledge_base_id}/shares", summary="列出知识库指定共享用户")
async def list_knowledge_shares(knowledge_base_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """仅创建者可查看授权名单。"""
    await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True)
    users = list((await db.scalars(select(KnowledgeShare.user_id).where(KnowledgeShare.knowledge_base_id == knowledge_base_id))).all())
    return api_response(
        data={
            "user_ids": users,
        },
    )


@router.delete("/{knowledge_base_id}/shares/{user_id}", summary="撤销指定用户的知识库权限")
async def revoke_knowledge_share(
    knowledge_base_id: int,
    user_id: int,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """当前权限即时生效，旧运行快照不能绕过撤销。"""
    await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True, lock=True)
    await db.execute(delete(KnowledgeShare).where(KnowledgeShare.knowledge_base_id == knowledge_base_id, KnowledgeShare.user_id == user_id))
    await cancel_knowledge_runs(db, knowledge_base_id, actor_id=user_id)
    audit(db, "knowledge.share_revoked", actor_id=actor.id, target_id=knowledge_base_id)
    await db.commit()
    return api_response(message="知识库共享已撤销")


async def _upload_knowledge_document(
    db,
    actor_id: int,
    knowledge_base_id: int,
    file: UploadFile,
    expected_revision: int,
    document_id: int | None = None,
    processing_options: str | None = None,
):
    """保存原始文件及上传版本和任务，解析与嵌入完全交给独立 worker。"""
    await require_knowledge_base(db, knowledge_base_id, actor_id, manage=True)
    try:
        options = KnowledgeDocumentOptions.model_validate_json(processing_options) if processing_options is not None else None
    except ValidationError:
        raise CustomException(status_code=422, custom_code=10006, detail="文档处理选项无效，请检查策略、工作表名称及表头行数") from None
    saved = None
    try:
        saved = await save_upload(file, actor_id, artifact_root(), max_bytes=10 * 1024 * 1024, allow_csv=True, allow_knowledge=True)
        artifact, _ = saved
        if artifact.media_type not in SUPPORTED_KNOWLEDGE_TYPES:
            raise CustomException(
                status_code=400,
                custom_code=10006,
                detail="知识文档支持 TXT、Markdown、PDF、DOCX、XLSX、CSV、HTML、JSON、YAML、PPTX；文本须使用 UTF-8",
            )
        item = await require_knowledge_base(db, knowledge_base_id, actor_id, manage=True, lock=True)
        if item.revision != expected_revision:
            raise CustomException(status_code=409, custom_code=10005, detail="知识库版本已变化")
        document = await db.get(KnowledgeDocument, document_id) if document_id is not None else None
        if document_id is not None and (document is None or document.knowledge_base_id != item.id or document.is_deleted != 0):
            raise CustomException(status_code=404, custom_code=10002, detail="知识文档不存在")
        if document is None:
            duplicate = await db.scalar(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.knowledge_base_id == item.id,
                    KnowledgeDocument.filename == artifact.filename,
                    KnowledgeDocument.is_deleted == 0,
                ),
            )
            document = duplicate
        if document is not None:
            previous = await db.scalar(
                select(KnowledgeDocumentVersion).where(
                    KnowledgeDocumentVersion.document_id == document.id,
                    KnowledgeDocumentVersion.version == document.current_version,
                ),
            )
            previous_options = KnowledgeDocumentOptions.model_validate(document.processing_options or {})
            if previous.sha256 == artifact.sha256 and (options is None or previous_options == options):
                await delete_blob(artifact_root(), artifact.storage_key)
                saved[1].unlink(missing_ok=True)
                return {
                    "document": document.to_dict(),
                    "revision": item.revision,
                    "deduplicated": True,
                    "job_id": None,
                }
            if document_id is None:
                raise CustomException(status_code=409, custom_code=10005, detail="同名文档已存在，请使用更新接口")
            document.current_version += 1
            document.filename = artifact.filename
        else:
            document = KnowledgeDocument(knowledge_base_id=item.id, filename=artifact.filename)
            db.add(document)
            await db.flush()
        effective_options = options or KnowledgeDocumentOptions.model_validate(document.processing_options or {})
        if artifact.media_type not in {"text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"} and (
            effective_options.sheet_names or effective_options.header_rows != 1
        ):
            raise CustomException(status_code=422, custom_code=10006, detail="工作表选择和多行表头仅适用于 XLSX / CSV")
        if artifact.media_type == "text/csv" and effective_options.sheet_names:
            raise CustomException(status_code=422, custom_code=10006, detail="CSV 没有工作表，请清空工作表选择")
        if artifact.media_type != "application/pdf" and effective_options.pdf_mode != KnowledgePdfMode.AUTO:
            raise CustomException(status_code=422, custom_code=10006, detail="PDF 提取方式仅适用于 PDF 文件")
        if options is not None:
            document.processing_options = options.model_dump(mode="json")
        await enforce_owner_quota(db, actor_id, add_count=1, add_bytes=artifact.byte_size)
        artifact.expires_at = None
        db.add(artifact)
        db.add(
            KnowledgeDocumentVersion(
                document_id=document.id,
                version=document.current_version,
                artifact_id=artifact.id,
                sha256=artifact.sha256,
                filename=artifact.filename,
                media_type=artifact.media_type,
                byte_size=artifact.byte_size,
            ),
        )
        job = await enqueue_knowledge_index(db, item, expected_revision)
        audit(db, "knowledge.document_uploaded", actor_id=actor_id, target_id=document.id)
        await db.commit()
        return {
            "document": document.to_dict(),
            "revision": item.revision,
            "job_id": job.id,
            "deduplicated": False,
        }
    except BaseException as exc:
        await db.rollback()
        if saved is not None:
            with suppress(Exception):
                await delete_blob(artifact_root(), saved[0].storage_key)
                saved[1].unlink(missing_ok=True)
        if isinstance(exc, ArtifactValidationError):
            raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
        raise


@router.post("/{knowledge_base_id}/documents", summary="上传知识文档并排队建立索引")
async def upload_knowledge_document(
    knowledge_base_id: int,
    file: UploadFile = File(description="单个 TXT、Markdown、PDF、DOCX、XLSX 或 UTF-8 CSV，最大 10 MiB"),
    expected_revision: int = Form(ge=1, description="当前知识库版本"),
    processing_options: str | None = Form(default=None, max_length=4096, description="文档处理覆盖选项 JSON：策略、工作表列表、表头行数及 PDF 页面提取方式"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """上传接口只保存私有文件和任务，不在 HTTP 请求内调用模型或解析大文件。"""
    result = await _upload_knowledge_document(db, actor.id, knowledge_base_id, file, expected_revision, processing_options=processing_options)
    return api_response(http_code=202, code=202, data=result, datetime_format=False)


@router.put("/{knowledge_base_id}/documents/{document_id}", summary="上传知识文档的新版本")
async def update_knowledge_document(
    knowledge_base_id: int,
    document_id: int,
    file: UploadFile = File(description="该文档的新版本原始文件"),
    expected_revision: int = Form(ge=1, description="当前知识库版本"),
    processing_options: str | None = Form(default=None, max_length=4096, description="文档处理覆盖选项 JSON：策略、工作表列表、表头行数及 PDF 页面提取方式"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """旧代在新版本完整索引成功前继续可用，Run 已固定代保持不变。"""
    result = await _upload_knowledge_document(db, actor.id, knowledge_base_id, file, expected_revision, document_id, processing_options)
    return api_response(http_code=202, code=202, data=result, datetime_format=False)


@router.get("/{knowledge_base_id}/documents", summary="列出知识库文档及当前上传版本")
async def list_knowledge_documents(knowledge_base_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """知识库最多一百份文档，列表不返回原文或内部文件存储键。"""
    await require_knowledge_base(db, knowledge_base_id, actor.id)
    rows = (
        await db.scalars(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == knowledge_base_id, KnowledgeDocument.is_deleted == 0)
            .order_by(KnowledgeDocument.id),
        )
    ).all()
    return api_response(data=[item.to_dict() for item in rows], is_pop=False, datetime_format=False)


@router.get("/{knowledge_base_id}/citations/{chunk_id}/page-image", summary="查看 PDF 引用对应的原始页面图片")
async def read_knowledge_pdf_page(
    knowledge_base_id: int,
    chunk_id: int,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按片段定位不可变版本，渲染前后鉴权，不暴露存储键且不缓存图片。"""
    citation = await read_knowledge_citation(db, actor.id, knowledge_base_id, chunk_id)
    if citation["page"] is None or (citation.get("source_locator") or {}).get("kind") != KnowledgeSourceKind.PDF:
        raise CustomException(status_code=404, custom_code=10002, detail="该引用没有 PDF 原页")
    version = await db.scalar(
        select(KnowledgeDocumentVersion).where(
            KnowledgeDocumentVersion.document_id == citation["document_id"],
            KnowledgeDocumentVersion.version == citation["document_version"],
        ),
    )
    artifact = await db.get(Artifact, version.artifact_id) if version is not None else None
    if artifact is None or artifact.is_deleted != 0 or artifact.state != ArtifactState.READY or version.media_type != "application/pdf":
        raise CustomException(status_code=404, custom_code=10002, detail="PDF 原始文件不可用")
    key, actor_id = artifact.storage_key, actor.id
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    await db.rollback()
    try:
        async with tool_execution_slot(sessions, actor_id, None, ToolLeaseKind.CALL, 45):
            content = await read_blob(artifact_root(), key, max_bytes=artifact_read_max_bytes())
            image = await asyncio.wait_for(render_pdf_page_isolated(content, citation["page"]), timeout=40)
    except ToolCapacityError:
        raise CustomException(status_code=429, custom_code=10005, detail="PDF 预览并发已满，请稍后重试") from None
    except (KnowledgeProcessingError, TimeoutError, OSError):
        raise CustomException(status_code=409, custom_code=10005, detail="PDF 原页暂不可读取，请检查文件或稍后重试") from None
    await read_knowledge_citation(db, actor_id, knowledge_base_id, chunk_id)
    return Response(
        image,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{knowledge_base_id}/documents/{document_id}/processing-preview", summary="分页预览已发布文档的原文证据及加工解释")
async def preview_knowledge_document(
    knowledge_base_id: int,
    document_id: int,
    after: int = Query(default=-1, ge=-1, description="上次已读取的片段位置，首次为 -1"),
    limit: int = Query(default=20, ge=1, le=50, description="本次最多返回的证据片段数"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """只读取活动代，原文、模型解释和表格来源分别返回，不触发解析或模型请求。"""
    result = await load_document_processing_preview(db, actor.id, knowledge_base_id, document_id, after, limit)
    response = api_response(data=result, datetime_format=False)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.delete("/{knowledge_base_id}/documents/{document_id}", summary="删除知识文档并阻断所有旧代引用")
async def delete_knowledge_document(
    knowledge_base_id: int,
    document_id: int,
    expected_revision: int = Query(ge=1),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """逻辑删除立即阻断旧代访问；模型无效时保留删除结果并提示修复后重建。"""
    item = await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True, lock=True)
    document = await db.get(KnowledgeDocument, document_id)
    if document is None or document.knowledge_base_id != item.id or document.is_deleted != 0:
        raise CustomException(status_code=404, custom_code=10002, detail="知识文档不存在")
    await _cancel_index_jobs(db, item.id)
    job, rebuild_reason = await remove_knowledge_document(db, item, document, expected_revision)
    await cancel_knowledge_runs(db, item.id)
    audit(db, "knowledge.document_deleted", actor_id=actor.id, target_id=document_id)
    await db.commit()
    return api_response(
        data={
            "revision": item.revision,
            "job_id": job.id if job is not None else None,
            "rebuild_required": job is None,
            "rebuild_reason": rebuild_reason,
        },
    )


@router.post("/{knowledge_base_id}/rebuild", summary="重建完整知识库索引代")
async def rebuild_knowledge_index(
    knowledge_base_id: int,
    body: KnowledgeRevisionIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """保存当前配置和完整文档版本清单，构建失败不替换既有活动代。"""
    item = await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True, lock=True)
    job = await enqueue_knowledge_index(db, item, body.expected_revision)
    await db.commit()
    return api_response(
        http_code=202,
        code=202,
        data={
            "revision": item.revision,
            "job_id": job.id,
        },
    )


@router.get("/{knowledge_base_id}/jobs", summary="列出知识库最近处理任务")
async def list_knowledge_jobs(knowledge_base_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """只向创建者返回最近五十项脱敏任务状态。"""
    await require_knowledge_base(db, knowledge_base_id, actor.id, manage=True)
    indices = select(KnowledgeIndex.id).where(KnowledgeIndex.knowledge_base_id == knowledge_base_id)
    jobs = (await db.scalars(select(KnowledgeJob).where(KnowledgeJob.index_id.in_(indices)).order_by(KnowledgeJob.id.desc()).limit(50))).all()
    return api_response(data=[job.to_dict(exclude={"lease_token"}) for job in jobs], is_pop=False, datetime_format=False)


@router.post("/{knowledge_base_id}/search", summary="有界检索知识库并返回固定原文引用")
async def search_knowledge_base(
    knowledge_base_id: int,
    body: KnowledgeSearchIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按当前活动代查询，混合检索失败时可显式选择只用关键词，不静默改变策略。"""
    item = await require_knowledge_base(db, knowledge_base_id, actor.id)
    indices = [item.active_index_id] if item.active_index_id is not None else []
    sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    try:
        timeout = get_config().KNOWLEDGE_SEARCH_TIMEOUT_SECONDS
        async with tool_execution_slot(sessions, actor.id, None, ToolLeaseKind.CALL, timeout):
            result = await asyncio.wait_for(search_knowledge(sessions, actor.id, indices, body), timeout=timeout)
    except ToolCapacityError:
        raise CustomException(status_code=429, custom_code=10005, detail="知识检索资源并发已满") from None
    except TimeoutError:
        raise CustomException(status_code=504, custom_code=10005, detail="知识检索超时") from None
    except ValueError:
        raise CustomException(status_code=409, custom_code=10005, detail="知识检索后端不可用或超过预算") from None
    return api_response(data=result, is_pop=False, datetime_format=False)


@router.get("/{knowledge_base_id}/citations/{chunk_id}", summary="按固定引用读取授权知识片段")
async def get_knowledge_citation(
    knowledge_base_id: int,
    chunk_id: int,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """每次读取重新验证当前权限和文档删除状态。"""
    result = await read_knowledge_citation(db, actor.id, knowledge_base_id, chunk_id)
    return api_response(data=result, is_pop=False, datetime_format=False)


@embedding_router.post("/{model_id}/embedding-test", summary="独立验证模型嵌入能力与实际维度")
async def test_embedding_model(model_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """文本能力不能替代嵌入验证，只保存实际通过的维度，不返回向量或凭据。"""
    from app.models.model_catalog import ModelCatalog

    started, failure, dimensions = time.monotonic(), None, None
    probed_identity = None
    try:
        model, provider = await load_embedding_model(db, model_id, verified=False)
        probed_identity = embedding_identity(model, provider)
        sessions = async_sessionmaker(db.bind, expire_on_commit=False)
        async with tool_execution_slot(sessions, actor.id, None, ToolLeaseKind.CALL, 60):
            result = await embed_texts(model, provider, ["知识检索验证", "文档引用核对"], dimensions=None)
        dimensions = len(result.vectors[0])
    except KnowledgeProcessingError as exc:
        failure = exc.code
    except ToolCapacityError:
        raise CustomException(status_code=429, custom_code=10005, detail="嵌入探测资源并发已满") from None
    model = await db.scalar(select(ModelCatalog).where(ModelCatalog.id == model_id).with_for_update().execution_options(populate_existing=True))
    if probed_identity is not None:
        from app.models.provider_connection import ProviderConnection

        provider = (
            await db.scalar(
                select(ProviderConnection).where(ProviderConnection.id == model.provider_id).with_for_update().execution_options(populate_existing=True),
            )
            if model is not None
            else None
        )
        if (
            model is None
            or provider is None
            or embedding_identity(model, provider) != probed_identity
            or model.status != 1
            or provider.status != 1
            or provider.is_deleted != 0
        ):
            raise CustomException(status_code=409, custom_code=10005, detail="探测期间模型配置已变化，请重新验证")
    if model is None or model.is_deleted != 0:
        raise CustomException(status_code=404, custom_code=10002, detail="模型不存在")
    verified = set(model.verified_capabilities or []) - {ModelCapability.EMBEDDING}
    if failure is None:
        verified.add(ModelCapability.EMBEDDING)
        model.capabilities = list(dict.fromkeys([*(model.capabilities or []), ModelCapability.EMBEDDING]))
    model.verified_capabilities = sorted(verified)
    model.embedding_dimensions = dimensions
    model.capabilities_verified = bool(model.capabilities) and set(model.capabilities).issubset(verified)
    audit(db, "model.embedding_tested", actor_id=actor.id, target_id=model_id)
    await db.commit()
    return api_response(
        data={
            "success": failure is None,
            "failure_code": failure,
            "dimensions": dimensions,
            "latency_ms": round((time.monotonic() - started) * 1000),
        },
    )


@retrieval_router.get("/{run_id}/knowledge-retrievals", summary="查询本轮实际使用的脱敏知识引用")
async def list_run_knowledge_retrievals(
    run_id: str,
    after_id: int = Query(default=0, ge=0),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """仅运行所有者读取有界审计，不返回查询正文、向量、文档原文或凭据。"""
    from app.services.agent_access import can_access_run_agent
    from app.services.run_manager import RunNotFoundError, get_owned_run

    try:
        run = await get_owned_run(db, run_id, actor.id)
    except RunNotFoundError:
        raise CustomException(status_code=404, custom_code=10002, detail="运行不存在") from None
    if not await can_access_run_agent(db, run.agent_id, actor.id):
        raise CustomException(status_code=404, custom_code=10002, detail="运行不存在")
    rows = (
        await db.scalars(
            select(KnowledgeRetrieval)
            .where(
                KnowledgeRetrieval.run_id == run_id,
                KnowledgeRetrieval.owner_id == actor.id,
                KnowledgeRetrieval.id > after_id,
            )
            .order_by(KnowledgeRetrieval.id)
            .limit(50),
        )
    ).all()
    return api_response(data=[row.to_dict() for row in rows], is_pop=False, datetime_format=False)
