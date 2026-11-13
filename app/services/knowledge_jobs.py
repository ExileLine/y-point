# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:52:30
# @Author  : yangyuexiong
# @File    : knowledge_jobs.py

"""知识索引任务的跨进程领取、阶段持久化、取消及原子发布。"""

import asyncio
from contextlib import suppress
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from loguru import logger
from sqlalchemy import bindparam, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import (
    ArtifactState,
    KnowledgeFailure,
    KnowledgeIndexState,
    KnowledgeJobState,
    KnowledgeSearchMode,
    KnowledgeProcessingMode,
    KnowledgePdfExtraction,
    KnowledgePdfLayout,
    ToolLeaseKind,
)
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.models.knowledge import (
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIndex,
    KnowledgeJob,
    KnowledgeEnrichmentCache,
)
from app.models.tool_execution import ToolExecutionLease
from app.services.artifact_store import artifact_read_max_bytes, artifact_root
from app.services.embedding_adapter import EMBEDDING_BATCH_SIZE, embed_texts, embedding_identity, load_embedding_model
from app.services.knowledge import MAX_INDEX_CHUNKS, TERMINAL_KNOWLEDGE_JOBS
from app.services.knowledge_enrichment import enrich_knowledge_chunks, load_processing_model, processing_fingerprint, processing_identity
from app.services.knowledge_formats import ROUTED_MEDIA_TYPES, ROUTED_PARSER_VERSION
from app.services.knowledge_tables import TABLE_MEDIA_TYPES, TABLE_PARSER_VERSION
from app.services.knowledge_pdf import PDF_PARSER_VERSION, pdf_runtime_configuration
from app.services.knowledge_backend import knowledge_tokens, remove_keyword_rows, write_keyword_rows
from app.services.knowledge_parser import KnowledgeProcessingError, ParsedKnowledge, chunk_knowledge, parse_knowledge_isolated
from app.services.private_blob_store import delete_blob, read_blob
from app.services.tool_execution import ToolCapacityError, acquire_tool_slot, release_tool_slot

KNOWLEDGE_JOB_TIMEOUT = 180
KNOWLEDGE_MAX_ATTEMPTS = 3


async def _job_stage(sessions, job_id: int, lease: str, state: KnowledgeJobState | None = None, *, skipped: bool = False) -> None:
    """每次阶段转换和心跳均核对最新期望代、用户、取消及租约所有权。"""
    async with sessions.begin() as db:
        job = await db.get(KnowledgeJob, job_id)
        index = await db.get(KnowledgeIndex, job.index_id) if job is not None else None
        kb = await db.get(KnowledgeBase, index.knowledge_base_id) if index is not None else None
        owner = await db.get(Admin, job.owner_id) if job is not None else None
        if (
            job is None
            or index is None
            or kb is None
            or owner is None
            or owner.is_deleted != 0
            or owner.status != 1
            or kb.is_deleted != 0
            or job.lease_token != lease
            or job.state == KnowledgeJobState.CANCEL_REQUESTED
            or kb.revision != index.revision
        ):
            raise KnowledgeProcessingError(KnowledgeFailure.CANCELLED)
        if job.state in TERMINAL_KNOWLEDGE_JOBS:
            raise KnowledgeProcessingError(KnowledgeFailure.SUPERSEDED)
        values = {
            "lease_expires_at": shanghai_now(naive=True) + timedelta(seconds=KNOWLEDGE_JOB_TIMEOUT + 30),
        }
        if state is not None:
            if not skipped:
                values["state"] = state
            values["progress"] = list(job.progress or []) + [
                {
                    "stage": state,
                    "skipped": skipped,
                    "time": shanghai_now().isoformat(),
                },
            ]
        changed = await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == job_id, KnowledgeJob.lease_token == lease, KnowledgeJob.state != KnowledgeJobState.CANCEL_REQUESTED)
            .values(**values),
        )
        if changed.rowcount != 1:
            raise KnowledgeProcessingError(KnowledgeFailure.CANCELLED)


async def _watch_job(sessions, job_id: int, lease: str) -> None:
    """外部模型或解析等待期间持续检测撤销和取消，不让阶段切换覆盖取消标记。"""
    while True:
        await asyncio.sleep(1)
        await _job_stage(sessions, job_id, lease)


async def _build_knowledge_index(sessions, job_id: int, lease: str) -> None:
    """按固定清单处理完整代，所有片段和活动指针在一次事务发布。"""
    await _job_stage(sessions, job_id, lease, KnowledgeJobState.PARSING)
    async with sessions() as db:
        job = await db.get(KnowledgeJob, job_id)
        index = await db.get(KnowledgeIndex, job.index_id)
        index_id, configuration = index.id, dict(index.configuration)
        version_ids = list(index.document_versions)
    parsed_versions = []
    for version_id in version_ids:
        await _job_stage(sessions, job_id, lease)
        async with sessions() as db:
            version = await db.get(KnowledgeDocumentVersion, version_id)
            document = await db.get(KnowledgeDocument, version.document_id)
            artifact = await db.get(Artifact, version.artifact_id)
            if document.is_deleted != 0 or artifact is None or artifact.state != ArtifactState.READY or artifact.is_deleted != 0:
                raise KnowledgeProcessingError(KnowledgeFailure.STORAGE_UNAVAILABLE)
            options = configuration.get("document_options", {}).get(str(document.id), {})
            is_pdf = version.media_type == "application/pdf"
            runtime = configuration.get("pdf_runtime") or pdf_runtime_configuration() if is_pdf else None
            fingerprint = processing_fingerprint(
                {
                    "parser": PDF_PARSER_VERSION if is_pdf else ROUTED_PARSER_VERSION if version.media_type in ROUTED_MEDIA_TYPES else TABLE_PARSER_VERSION,
                    "pdf_runtime": runtime,
                    "media_type": version.media_type,
                    "sha256": version.sha256,
                    "options": options,
                },
            )
            if (
                version.extracted_text is not None
                and version.blocks is not None
                and (
                    version.parser_fingerprint == fingerprint
                    or version.parser_fingerprint is None
                    and version.media_type not in (*TABLE_MEDIA_TYPES, *ROUTED_MEDIA_TYPES, "application/pdf")
                )
            ):
                parsed = ParsedKnowledge(version.extracted_text, version.blocks, version.processing_metadata or {})
            else:
                content = await read_blob(artifact_root(), artifact.storage_key, max_bytes=artifact_read_max_bytes())
                parsed = None
        if parsed is None:
            if is_pdf:
                options = {
                    **options,
                    "_pdf_runtime": runtime,
                }
            parsed = (
                await parse_knowledge_isolated(content, version.media_type, options)
                if version.media_type in TABLE_MEDIA_TYPES or version.media_type in ROUTED_MEDIA_TYPES or is_pdf
                else await parse_knowledge_isolated(content, version.media_type)
            )
            async with sessions.begin() as db:
                await db.execute(
                    update(KnowledgeDocumentVersion)
                    .where(KnowledgeDocumentVersion.id == version_id)
                    .values(
                        processing_metadata=parsed.metadata,
                        extracted_text=parsed.text,
                        blocks=parsed.blocks,
                        parser_fingerprint=fingerprint,
                    ),
                )
        parsed_versions.append((version, parsed))
    await _job_stage(sessions, job_id, lease, KnowledgeJobState.CHUNKING)
    chunks = []
    for version, parsed in parsed_versions:
        pieces = await asyncio.to_thread(chunk_knowledge, parsed, configuration["chunk_size"], configuration["chunk_overlap"])
        for piece in pieces:
            chunks.append(
                KnowledgeChunk(
                    index_id=index_id,
                    document_id=version.document_id,
                    document_version_id=version.id,
                    search_terms=" ".join(knowledge_tokens(piece["content"])),
                    **piece,
                ),
            )
        if len(chunks) > MAX_INDEX_CHUNKS:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)

    async def check_active():
        """外部加工每批请求前后复核租约、取消、用户和最新索引代。"""
        await _job_stage(sessions, job_id, lease)

    enhanced = []
    for version, _ in parsed_versions:
        mode = configuration.get("document_options", {}).get(str(version.document_id), {}).get("processing_mode") or configuration.get(
            "processing_mode",
            KnowledgeProcessingMode.ORIGINAL,
        )
        supported = version.media_type in TABLE_MEDIA_TYPES or version.media_type in ROUTED_MEDIA_TYPES or version.media_type == "application/pdf"
        complex_pdf = version.media_type == "application/pdf" and any(
            chunk.document_version_id == version.id
            and chunk.source_locator
            and (chunk.source_locator.get("extraction_method") == KnowledgePdfExtraction.OCR or chunk.source_locator.get("layout") == KnowledgePdfLayout.TABLE)
            for chunk in chunks
        )
        if supported and (
            mode == KnowledgeProcessingMode.ENHANCED
            or mode == KnowledgeProcessingMode.AUTO
            and configuration.get("processing_identity")
            and (version.media_type in TABLE_MEDIA_TYPES or complex_pdf)
        ):
            enhanced.append(version.id)
    await _job_stage(sessions, job_id, lease, KnowledgeJobState.ENRICHING, skipped=not enhanced)
    for version_id in enhanced:
        await enrich_knowledge_chunks(
            sessions,
            job_id,
            lease,
            configuration,
            [chunk for chunk in chunks if chunk.document_version_id == version_id],
            check_active,
        )
    for chunk in chunks:
        chunk.search_terms = " ".join(knowledge_tokens(chunk.retrieval_text or chunk.content))
    async with sessions() as db:
        previous = await db.get(KnowledgeJob, job_id)
        tokens, cost, reserved = previous.embedding_tokens, previous.estimated_cost, previous.reserved_tokens
    if configuration["search_mode"] == KnowledgeSearchMode.HYBRID and chunks:
        await _job_stage(sessions, job_id, lease, KnowledgeJobState.EMBEDDING)
        async with sessions() as db:
            identity = configuration["embedding_identity"]
            model, provider = await load_embedding_model(db, identity["model_id"])
            if embedding_identity(model, provider) != identity:
                raise KnowledgeProcessingError(KnowledgeFailure.MODEL_UNAVAILABLE)
        conservative_tokens = sum(len((chunk.retrieval_text or chunk.content).encode()) for chunk in chunks)
        budget = configuration["embedding_cost_budget"]
        token_budget = configuration["embedding_token_budget"]
        if (
            token_budget is not None
            and conservative_tokens + reserved > token_budget
            or budget is not None
            and (model.input_price is None or Decimal(conservative_tokens + reserved) * model.input_price / Decimal(1000000) > Decimal(budget))
        ):
            raise KnowledgeProcessingError(KnowledgeFailure.BUDGET_EXCEEDED)
        for offset in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            await _job_stage(sessions, job_id, lease)
            batch = chunks[offset : offset + EMBEDDING_BATCH_SIZE]
            reserved += sum(len((chunk.retrieval_text or chunk.content).encode()) for chunk in batch)
            async with sessions.begin() as db:
                changed = await db.execute(
                    update(KnowledgeJob)
                    .where(
                        KnowledgeJob.id == job_id,
                        KnowledgeJob.lease_token == lease,
                        KnowledgeJob.state == KnowledgeJobState.EMBEDDING,
                    )
                    .values(reserved_tokens=reserved),
                )
                if changed.rowcount != 1:
                    raise KnowledgeProcessingError(KnowledgeFailure.CANCELLED)
            result = await embed_texts(model, provider, [chunk.retrieval_text or chunk.content for chunk in batch], dimensions=identity["dimensions"])
            for chunk, vector in zip(batch, result.vectors, strict=True):
                chunk.embedding = vector
            tokens += result.tokens or 0
            if result.cost is not None:
                cost = (cost or Decimal(0)) + result.cost
            async with sessions.begin() as db:
                await db.execute(
                    update(KnowledgeJob)
                    .where(KnowledgeJob.id == job_id, KnowledgeJob.lease_token == lease)
                    .values(embedding_tokens=tokens, estimated_cost=cost),
                )
            if (token_budget is not None and tokens > token_budget) or (budget is not None and cost is not None and cost > Decimal(budget)):
                raise KnowledgeProcessingError(KnowledgeFailure.BUDGET_EXCEEDED)
    else:
        await _job_stage(sessions, job_id, lease, KnowledgeJobState.EMBEDDING, skipped=True)
    await _job_stage(sessions, job_id, lease, KnowledgeJobState.INDEXING)
    async with sessions.begin() as db:
        job = await db.scalar(select(KnowledgeJob).where(KnowledgeJob.id == job_id).with_for_update())
        index = await db.get(KnowledgeIndex, index_id)
        kb = await db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == index.knowledge_base_id).with_for_update())
        owner = await db.get(Admin, job.owner_id)
        if (
            owner is None
            or owner.status != 1
            or owner.is_deleted != 0
            or job.lease_token != lease
            or job.state != KnowledgeJobState.INDEXING
            or kb.is_deleted != 0
            or kb.revision != index.revision
        ):
            raise KnowledgeProcessingError(KnowledgeFailure.CANCELLED)
        if enhanced:
            identity = configuration["processing_identity"]
            model, provider = await load_processing_model(db, identity["model_id"])
            if processing_identity(model, provider) != identity:
                raise KnowledgeProcessingError(KnowledgeFailure.MODEL_UNAVAILABLE)
            token_budget = configuration["processing_token_budget"]
            if (token_budget is not None and (job.processing_tokens or 0) > token_budget) or (
                configuration["processing_cost_budget"] is not None
                and job.processing_estimated_cost is not None
                and job.processing_estimated_cost > Decimal(configuration["processing_cost_budget"])
            ):
                raise KnowledgeProcessingError(KnowledgeFailure.BUDGET_EXCEEDED)
        await remove_keyword_rows(db, index_id)
        db.add_all(chunks)
        await db.flush()
        await write_keyword_rows(db, chunks)
        index.processing_metadata = {str(version.id): parsed.metadata for version, parsed in parsed_versions if parsed.metadata}
        index.state, index.chunk_count = KnowledgeIndexState.READY, len(chunks)
        kb.active_index_id = index.id
        job.state, job.failure_code = KnowledgeJobState.COMPLETED, None
        job.progress = list(job.progress or []) + [
            {
                "stage": KnowledgeJobState.COMPLETED,
                "time": shanghai_now().isoformat(),
            },
        ]
        job.lease_token, job.lease_expires_at = None, None


async def _fail_knowledge_job(sessions, job_id: int, lease: str, code: KnowledgeFailure) -> None:
    """失败只清理本次未发布代，保留已经发布的活动索引与其原文。"""
    async with sessions.begin() as db:
        job = await db.scalar(select(KnowledgeJob).where(KnowledgeJob.id == job_id, KnowledgeJob.lease_token == lease).with_for_update())
        if job is None or job.state == KnowledgeJobState.COMPLETED:
            return
        index = await db.get(KnowledgeIndex, job.index_id)
        if job.state == KnowledgeJobState.CANCEL_REQUESTED:
            code = KnowledgeFailure.CANCELLED
        cancelled = code in (KnowledgeFailure.CANCELLED, KnowledgeFailure.SUPERSEDED)
        retry = code == KnowledgeFailure.WORKER_INTERRUPTED and job.attempts < KNOWLEDGE_MAX_ATTEMPTS
        job.state = KnowledgeJobState.UPLOADED if retry else KnowledgeJobState.CANCELLED if cancelled else KnowledgeJobState.FAILED
        job.failure_code = code
        job.progress = list(job.progress or []) + [
            {
                "stage": job.state,
                "failure_code": code,
                "time": shanghai_now().isoformat(),
            },
        ]
        job.lease_token, job.lease_expires_at = None, None
        index.state = KnowledgeIndexState.BUILDING if retry else KnowledgeIndexState.CANCELLED if cancelled else KnowledgeIndexState.FAILED
        await remove_keyword_rows(db, index.id)


async def process_next_knowledge_job(sessions: async_sessionmaker[AsyncSession]) -> bool:
    """原子领取一个持久化任务，跨进程复用资源名额并处理到期重试。"""
    now = shanghai_now(naive=True)
    async with sessions.begin() as db:
        expired = (
            await db.scalars(select(KnowledgeJob).where(KnowledgeJob.lease_expires_at <= now, KnowledgeJob.lease_token.is_not(None)).with_for_update())
        ).all()
        for previous in expired:
            index = await db.get(KnowledgeIndex, previous.index_id)
            cancelled = previous.state == KnowledgeJobState.CANCEL_REQUESTED
            retry = previous.attempts < KNOWLEDGE_MAX_ATTEMPTS and not cancelled
            previous.state = KnowledgeJobState.UPLOADED if retry else KnowledgeJobState.CANCELLED if cancelled else KnowledgeJobState.FAILED
            previous.failure_code = KnowledgeFailure.CANCELLED if cancelled else KnowledgeFailure.WORKER_INTERRUPTED
            previous.lease_token, previous.lease_expires_at = None, None
            if not retry:
                index.state = KnowledgeIndexState.BUILDING if retry else KnowledgeIndexState.CANCELLED if cancelled else KnowledgeIndexState.FAILED
            previous.progress = list(previous.progress or []) + [
                {
                    "stage": previous.state,
                    "failure_code": previous.failure_code,
                    "time": shanghai_now().isoformat(),
                },
            ]
        await db.flush()
        job = await db.scalar(
            select(KnowledgeJob)
            .where(
                or_(
                    KnowledgeJob.state == KnowledgeJobState.UPLOADED,
                    (KnowledgeJob.state == KnowledgeJobState.CANCEL_REQUESTED) & KnowledgeJob.lease_token.is_(None),
                ),
            )
            .order_by(KnowledgeJob.id)
            .limit(1),
        )
        if job is None:
            return False
        try:
            slot = await acquire_tool_slot(db, job.owner_id, None, ToolLeaseKind.PREPARATION, KNOWLEDGE_JOB_TIMEOUT)
        except ToolCapacityError:
            return False
        lease, job_id = str(uuid4()), job.id
        claimed = await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == job.id, KnowledgeJob.lease_token.is_(None), KnowledgeJob.state == job.state)
            .values(lease_token=lease, lease_expires_at=now + timedelta(seconds=KNOWLEDGE_JOB_TIMEOUT + 30), attempts=KnowledgeJob.attempts + 1),
        )
        if claimed.rowcount != 1:
            from sqlalchemy import delete

            await db.execute(delete(ToolExecutionLease).where(ToolExecutionLease.token == slot))
            return False
    task, watcher = (
        asyncio.create_task(_build_knowledge_index(sessions, job_id, lease)),
        asyncio.create_task(_watch_job(sessions, job_id, lease)),
    )
    try:
        async with asyncio.timeout(KNOWLEDGE_JOB_TIMEOUT):
            completed, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            for finished in completed:
                await finished
    except BaseException as exc:
        code = (
            exc.code
            if isinstance(exc, KnowledgeProcessingError)
            else KnowledgeFailure.WORKER_INTERRUPTED
            if isinstance(exc, asyncio.CancelledError)
            else KnowledgeFailure.TIMEOUT
            if isinstance(exc, TimeoutError)
            else KnowledgeFailure.PARSE_FAILED
        )
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _fail_knowledge_job(sessions, job_id, lease, code)
        if isinstance(exc, asyncio.CancelledError):
            raise
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError, KnowledgeProcessingError):
            await watcher
        await release_tool_slot(sessions, slot)
    return True


async def prune_deleted_knowledge_blobs(sessions) -> int:
    """逻辑删除先阻断读取，后台有界清除原始 Blob 并保留脱敏引用元数据。"""
    async with sessions() as db:
        artifacts = (
            await db.scalars(
                select(Artifact)
                .join(KnowledgeDocumentVersion, KnowledgeDocumentVersion.artifact_id == Artifact.id)
                .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeDocumentVersion.document_id)
                .join(KnowledgeBase, KnowledgeBase.id == KnowledgeDocument.knowledge_base_id)
                .where(or_(KnowledgeDocument.is_deleted != 0, KnowledgeBase.is_deleted != 0), Artifact.state == ArtifactState.READY)
                .limit(100),
            )
        ).all()
    removed = 0
    for artifact in artifacts:
        try:
            await delete_blob(artifact_root(), artifact.storage_key)
            async with sessions.begin() as db:
                await db.execute(update(Artifact).where(Artifact.id == artifact.id).values(state=ArtifactState.DELETED, is_deleted=1))
            removed += 1
        except Exception as exc:
            logger.warning("知识原文清理延后：{}", type(exc).__name__)
    async with sessions.begin() as db:
        deleted_documents = (
            select(KnowledgeDocument.id)
            .join(
                KnowledgeBase,
                KnowledgeBase.id == KnowledgeDocument.knowledge_base_id,
            )
            .where(or_(KnowledgeDocument.is_deleted != 0, KnowledgeBase.is_deleted != 0))
        )
        chunk_ids = list(
            (
                await db.scalars(
                    select(KnowledgeChunk.id)
                    .where(
                        KnowledgeChunk.document_id.in_(deleted_documents),
                    )
                    .limit(2000),
                )
            ).all(),
        )
        if chunk_ids:
            if db.bind.dialect.name == "sqlite":
                statement = text("DELETE FROM yp_knowledge_fts WHERE chunk_id IN :ids").bindparams(bindparam("ids", expanding=True))
                await db.execute(
                    statement,
                    {
                        "ids": chunk_ids,
                    },
                )
            await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.id.in_(chunk_ids)))
        version_ids = list(
            (
                await db.scalars(
                    select(KnowledgeDocumentVersion.id)
                    .where(
                        KnowledgeDocumentVersion.document_id.in_(deleted_documents),
                        KnowledgeDocumentVersion.extracted_text.is_not(None),
                    )
                    .limit(100),
                )
            ).all(),
        )
        if version_ids:
            await db.execute(delete(KnowledgeEnrichmentCache).where(KnowledgeEnrichmentCache.document_version_id.in_(version_ids)))
            await db.execute(
                update(KnowledgeDocumentVersion)
                .where(KnowledgeDocumentVersion.id.in_(version_ids))
                .values(extracted_text=None, blocks=None, processing_metadata={})
            )
    return removed
