# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:50:37
# @Author  : yangyuexiong
# @File    : knowledge.py

"""知识库资源授权、不可变索引调度、检索与引用的统一业务接口。"""

import hashlib
import json
import time
from collections import defaultdict

from sqlalchemy import literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import KnowledgeIndexState, KnowledgeJobState, KnowledgeRerank, KnowledgeSearchMode
from app.models.admin import Admin
from app.models.base import shanghai_now
from app.models.knowledge import (
    AgentKnowledgeBase,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeDocumentVersion,
    KnowledgeIndex,
    KnowledgeJob,
    KnowledgeRetrieval,
    KnowledgeShare,
    RunKnowledgeBase,
)
from app.schemas.knowledge import KnowledgeSearchIn
from app.services.embedding_adapter import embed_texts, embedding_identity, load_embedding_model
from app.services.knowledge_backend import keyword_candidates, knowledge_tokens, vector_candidates
from app.services.knowledge_parser import KnowledgeProcessingError

MAX_KNOWLEDGE_DOCUMENTS = 100
MAX_KNOWLEDGE_BYTES = 50 * 1024 * 1024
MAX_INDEX_CHUNKS = 2000
TERMINAL_KNOWLEDGE_JOBS = frozenset({KnowledgeJobState.COMPLETED, KnowledgeJobState.FAILED, KnowledgeJobState.CANCELLED})


def knowledge_access(actor_id: int):
    """生成实际用户的只读访问条件，Agent 共享不参与授权。"""
    shared = select(KnowledgeShare.knowledge_base_id).where(KnowledgeShare.user_id == actor_id, KnowledgeShare.is_deleted == 0)
    active_owners = select(Admin.id).where(Admin.status == 1, Admin.is_deleted == 0)
    return (
        (KnowledgeBase.is_deleted == 0)
        & (KnowledgeBase.status == 1)
        & KnowledgeBase.owner_id.in_(active_owners)
        & literal(actor_id).in_(active_owners)
        & or_(KnowledgeBase.owner_id == actor_id, KnowledgeBase.id.in_(shared))
    )


async def require_knowledge_base(
    db: AsyncSession, identifier: int, actor_id: int, *, manage: bool = False, lock: bool = False
) -> KnowledgeBase:
    """不存在、删除和未授权统一隐藏，管理仅允许创建者。"""
    statement = select(KnowledgeBase).where(KnowledgeBase.id == identifier, knowledge_access(actor_id))
    if manage:
        statement = statement.where(KnowledgeBase.owner_id == actor_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    item = await db.scalar(statement)
    if item is None:
        raise CustomException(status_code=404, custom_code=10002, detail="知识库不存在")
    return item


async def validate_knowledge_configuration(db: AsyncSession, values: dict) -> dict:
    """混合模式必须独立验证嵌入模型，并固定影响索引的身份和预算。"""
    configuration = {
        key: str(values[key]) if key == "embedding_cost_budget" and values[key] is not None else values[key]
        for key in ("search_mode", "embedding_model_id", "chunk_size", "chunk_overlap", "embedding_token_budget", "embedding_cost_budget")
    }
    if configuration["search_mode"] == KnowledgeSearchMode.HYBRID:
        try:
            model, provider = await load_embedding_model(db, configuration["embedding_model_id"])
        except KnowledgeProcessingError:
            raise CustomException(status_code=409, custom_code=10005, detail="嵌入模型未通过独立验证或当前不可用") from None
        if configuration["embedding_cost_budget"] is not None and model.input_price is None:
            raise CustomException(status_code=409, custom_code=10005, detail="启用嵌入费用预算前须配置输入价格")
        configuration["embedding_identity"] = embedding_identity(model, provider)
    return configuration


async def enqueue_knowledge_index(db: AsyncSession, item: KnowledgeBase, expected_revision: int) -> KnowledgeJob:
    """先 CAS 更新期望代，再保存完整文档版本清单和持久化调度意图。"""
    changed = await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id == item.id, KnowledgeBase.revision == expected_revision, KnowledgeBase.is_deleted == 0)
        .values(revision=expected_revision + 1)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="知识库版本已变化")
    await db.flush()
    versions = (
        await db.scalars(
            select(KnowledgeDocumentVersion)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeDocumentVersion.document_id)
            .where(
                KnowledgeDocument.knowledge_base_id == item.id,
                KnowledgeDocument.is_deleted == 0,
                KnowledgeDocument.current_version == KnowledgeDocumentVersion.version,
            )
            .order_by(KnowledgeDocument.id)
        )
    ).all()
    if len(versions) > MAX_KNOWLEDGE_DOCUMENTS or sum(version.byte_size for version in versions) > MAX_KNOWLEDGE_BYTES:
        raise CustomException(status_code=413, custom_code=10005, detail="知识库文档数量或总容量超限")
    configuration = await validate_knowledge_configuration(db, {column.key: getattr(item, column.key) for column in item.__table__.columns})
    index = KnowledgeIndex(
        knowledge_base_id=item.id,
        revision=expected_revision + 1,
        configuration=configuration,
        document_versions=[version.id for version in versions],
        state=KnowledgeIndexState.BUILDING,
    )
    db.add(index)
    await db.flush()
    job = KnowledgeJob(
        index_id=index.id,
        owner_id=item.owner_id,
        state=KnowledgeJobState.UPLOADED,
        progress=[
            {
                "stage": KnowledgeJobState.UPLOADED,
                "time": shanghai_now().isoformat(),
            }
        ],
    )
    db.add(job)
    await db.flush()
    item.revision = expected_revision + 1
    return job


async def selected_knowledge_bases(db: AsyncSession, identifiers: list[int], actor_id: int) -> list[KnowledgeBase]:
    """Agent 只能选择实际可用且无重复的知识库，最多八个。"""
    if len(identifiers) > 8 or len(set(identifiers)) != len(identifiers):
        raise CustomException(status_code=400, custom_code=10006, detail="知识库列表重复或超过八个")
    return [await require_knowledge_base(db, identifier, actor_id) for identifier in identifiers]


async def agent_knowledge_ids(db: AsyncSession, agent_id: int) -> list[int]:
    """返回 Agent 关联 ID，不向共享 Agent 使用者授予隐含读取权限。"""
    return list(
        (
            await db.scalars(
                select(AgentKnowledgeBase.knowledge_base_id).where(AgentKnowledgeBase.agent_id == agent_id).order_by(AgentKnowledgeBase.id)
            )
        ).all(),
    )


async def snapshot_run_knowledge(db: AsyncSession, run_id: str, agent_id: int, actor_id: int) -> None:
    """提交时仅固定实际用户可见资源的当前完整索引代。"""
    identifiers = await agent_knowledge_ids(db, agent_id)
    if identifiers:
        items = (await db.scalars(select(KnowledgeBase).where(KnowledgeBase.id.in_(identifiers), knowledge_access(actor_id)))).all()
        db.add_all([RunKnowledgeBase(run_id=run_id, knowledge_base_id=item.id, index_id=item.active_index_id) for item in items])


async def cancel_knowledge_runs(db: AsyncSession, knowledge_base_id: int, *, actor_id: int | None = None) -> None:
    """资源删除或撤权时取消正在运行、排队或等待审批的受影响运行。"""
    from app.models.agent_run import AgentRun
    from app.services.run_manager import TERMINAL_STATES, request_cancel

    statement = (
        select(AgentRun)
        .join(RunKnowledgeBase, RunKnowledgeBase.run_id == AgentRun.id)
        .where(RunKnowledgeBase.knowledge_base_id == knowledge_base_id, AgentRun.state.not_in(TERMINAL_STATES))
    )
    if actor_id is not None:
        statement = statement.where(AgentRun.owner_id == actor_id)
    for run in (await db.scalars(statement)).all():
        await request_cancel(db, run.id, run.owner_id)


async def allowed_knowledge_indices(db: AsyncSession, actor_id: int, requested: list[int]) -> list[KnowledgeIndex]:
    """在每次阶段读取时复核当前授权，限定完整已发布的固定索引。"""
    if not requested:
        return []
    return list(
        (
            await db.scalars(
                select(KnowledgeIndex)
                .join(KnowledgeBase, KnowledgeBase.id == KnowledgeIndex.knowledge_base_id)
                .where(KnowledgeIndex.id.in_(requested), KnowledgeIndex.state == KnowledgeIndexState.READY, knowledge_access(actor_id))
            )
        ).all(),
    )


def _citation(chunk: KnowledgeChunk, version: KnowledgeDocumentVersion, index: KnowledgeIndex) -> dict:
    """返回可定位和重新鉴权的固定片段，不伪造 DOCX 页码。"""
    return {
        "citation": f"kb:{index.knowledge_base_id}:i:{index.id}:c:{chunk.id}",
        "knowledge_base_id": index.knowledge_base_id,
        "index_id": index.id,
        "document_id": chunk.document_id,
        "document_version": version.version,
        "chunk_id": chunk.id,
        "filename": version.filename,
        "sha256": version.sha256,
        "start_offset": chunk.start_offset,
        "end_offset": chunk.end_offset,
        "page": chunk.page,
        "heading": chunk.heading,
        "content": chunk.content,
    }


async def read_knowledge_citation(db: AsyncSession, actor_id: int, knowledge_base_id: int, chunk_id: int) -> dict:
    """片段读取始终重新核对知识库与文档删除状态，旧引用不能绕过撤权。"""
    await require_knowledge_base(db, knowledge_base_id, actor_id)
    row = (
        await db.execute(
            select(KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeIndex)
            .join(KnowledgeDocumentVersion, KnowledgeDocumentVersion.id == KnowledgeChunk.document_version_id)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .join(KnowledgeIndex, KnowledgeIndex.id == KnowledgeChunk.index_id)
            .where(
                KnowledgeChunk.id == chunk_id,
                KnowledgeIndex.knowledge_base_id == knowledge_base_id,
                KnowledgeIndex.state == KnowledgeIndexState.READY,
                KnowledgeDocument.is_deleted == 0,
            )
        )
    ).first()
    if row is None:
        raise CustomException(status_code=404, custom_code=10002, detail="知识引用不存在")
    return _citation(*row)


async def search_knowledge(
    sessions: async_sessionmaker[AsyncSession],
    actor_id: int,
    index_ids: list[int],
    body: KnowledgeSearchIn,
    *,
    run_id: str | None = None,
) -> dict:
    """固定代检索，授权先于召回，外部嵌入后再次鉴权并写脱敏实际引用审计。"""
    if not body.query.strip():
        raise CustomException(status_code=400, custom_code=10006, detail="检索问题不能为空白")
    started = time.monotonic()
    ranks = defaultdict(float)
    embedding_usage = []
    async with sessions() as db:
        indices = await allowed_knowledge_indices(db, actor_id, index_ids)
        valid_ids = [index.id for index in indices]
        keyword = await keyword_candidates(db, valid_ids, body.query, 80)
        for rank, (identifier, _) in enumerate(keyword, 1):
            ranks[identifier] += 1 / (60 + rank)
    grouped = defaultdict(list)
    if not body.keyword_only:
        for index in indices:
            if index.configuration["search_mode"] == KnowledgeSearchMode.HYBRID:
                grouped[json.dumps(index.configuration["embedding_identity"], sort_keys=True)].append(index.id)
    for identity_json, group_ids in grouped.items():
        identity = json.loads(identity_json)
        try:
            async with sessions() as db:
                model, provider = await load_embedding_model(db, identity["model_id"])
                if embedding_identity(model, provider) != identity:
                    raise CustomException(status_code=409, custom_code=10005, detail="嵌入模型身份已变化，请重建索引")
            result = await embed_texts(model, provider, [body.query], dimensions=identity["dimensions"])
        except KnowledgeProcessingError as exc:
            raise CustomException(status_code=409, custom_code=10005, detail=f"知识检索暂不可用：{exc.code}") from None
        embedding_usage.append(
            {
                "model_id": model.id,
                "dimensions": identity["dimensions"],
                "tokens": result.tokens,
                "cost": str(result.cost) if result.cost is not None else None,
            }
        )
        async with sessions() as db:
            current = await allowed_knowledge_indices(db, actor_id, group_ids)
            candidates = await vector_candidates(
                db,
                [index.id for index in current],
                result.vectors[0],
                80,
                body.min_similarity,
                get_config().KNOWLEDGE_VECTOR_BACKEND,
            )
        for rank, (identifier, _) in enumerate(candidates, 1):
            ranks[identifier] += 1 / (60 + rank)
    hits, byte_count = [], 0
    seen_content = set()
    async with sessions.begin() as db:
        current = await allowed_knowledge_indices(db, actor_id, valid_ids)
        by_index = {index.id: index for index in current}
        rows = (
            (
                await db.execute(
                    select(KnowledgeChunk, KnowledgeDocumentVersion)
                    .join(KnowledgeDocumentVersion, KnowledgeDocumentVersion.id == KnowledgeChunk.document_version_id)
                    .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
                    .where(
                        KnowledgeChunk.id.in_(list(ranks)), KnowledgeChunk.index_id.in_(list(by_index)), KnowledgeDocument.is_deleted == 0
                    )
                )
            ).all()
            if ranks
            else []
        )
        query_terms = set(knowledge_tokens(body.query))
        if body.rerank == KnowledgeRerank.COVERAGE:
            for chunk, _ in rows:
                ranks[chunk.id] += len(query_terms & set(chunk.search_terms.split())) / max(1, len(query_terms))
        for chunk, version in sorted(rows, key=lambda row: (-ranks[row[0].id], row[0].id)):
            digest = hashlib.sha256(chunk.content.encode()).digest()
            if digest in seen_content:
                continue
            size = len(chunk.content.encode())
            if size + byte_count > body.max_bytes or len(hits) >= body.top_k:
                continue
            hit = _citation(chunk, version, by_index[chunk.index_id])
            hit["score"] = ranks[chunk.id]
            hits.append(hit)
            seen_content.add(digest)
            byte_count += size
        if run_id is not None:
            from app.models.agent_run import AgentRun
            from app.core.enums import RunState

            run = await db.get(AgentRun, run_id)
            if run is None or run.owner_id != actor_id or run.state != RunState.RUNNING:
                raise CustomException(status_code=409, custom_code=10005, detail="运行已结束或授权已失效")
            db.add(
                KnowledgeRetrieval(
                    run_id=run_id,
                    owner_id=actor_id,
                    query_hash=hashlib.sha256(body.query.encode()).hexdigest(),
                    references=[
                        {key: hit[key] for key in ("knowledge_base_id", "index_id", "chunk_id", "document_id", "document_version")}
                        for hit in hits
                    ],
                    embedding_usage=embedding_usage,
                    hit_count=len(hits),
                    byte_count=byte_count,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
            )
    return {
        "hits": hits,
        "no_answer": not hits,
        "byte_count": byte_count,
        "untrusted_data": True,
    }
