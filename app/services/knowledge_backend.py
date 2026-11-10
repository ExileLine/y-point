# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:47:27
# @Author  : yangyuexiong
# @File    : knowledge_backend.py

"""跨数据库原生全文检索、中文稳定词项及可替换向量距离计算。"""

import math
import re
import unicodedata

from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import KnowledgeVectorBackend
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument


def knowledge_tokens(content: str) -> list[str]:
    """中文按字符二元切分，词项编码避免各后端最小词长及停用词差异。"""
    tokens = set()
    normalized = unicodedata.normalize("NFKC", content).casefold()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9_]+", normalized):
        if "\u3400" <= part[0] <= "\u9fff":
            pieces = [part[index : index + 2] for index in range(len(part) - 1)] if len(part) > 1 else [part]
        else:
            pieces = [part]
        bounded = [piece[start : start + 32] for piece in pieces for start in range(0, len(piece), 32)]
        for piece in bounded:
            tokens.add("t" + piece.encode().hex())
    return sorted(tokens)


def create_native_keyword_index(connection) -> None:
    """创建对应方言的全文设施，向量扩展不影响数据库初始化。"""
    dialect = connection.dialect.name
    if dialect == "sqlite":
        connection.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS yp_knowledge_fts USING fts5(terms, index_id UNINDEXED, chunk_id UNINDEXED)")
    elif dialect == "postgresql":
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_yp_knowledge_native_terms ON yp_knowledge_chunk USING gin (to_tsvector('simple', search_terms))",
        )
    elif dialect == "mysql":
        connection.exec_driver_sql("CREATE FULLTEXT INDEX ix_yp_knowledge_native_terms ON yp_knowledge_chunk (search_terms)")


async def write_keyword_rows(db: AsyncSession, chunks: list[KnowledgeChunk]) -> None:
    """SQLite 在同一发布事务写入 FTS，其他后端由原生索引自动维护。"""
    if db.bind.dialect.name == "sqlite" and chunks:
        await db.execute(
            text("INSERT INTO yp_knowledge_fts (terms, index_id, chunk_id) VALUES (:terms, :index_id, :chunk_id)"),
            [
                {
                    "terms": chunk.search_terms,
                    "index_id": chunk.index_id,
                    "chunk_id": chunk.id,
                }
                for chunk in chunks
            ],
        )


async def remove_keyword_rows(db: AsyncSession, index_id: int) -> None:
    """清除未发布代的关键词和片段，历史发布代不在此处自动淘汰。"""
    if db.bind.dialect.name == "sqlite":
        await db.execute(
            text("DELETE FROM yp_knowledge_fts WHERE index_id = :index_id"),
            {
                "index_id": index_id,
            },
        )
    await db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.index_id == index_id))


async def keyword_candidates(db: AsyncSession, index_ids: list[int], query: str, limit: int) -> list[tuple[int, float]]:
    """在已授权索引和未删除文档内执行原生全文检索，返回有界候选。"""
    terms = knowledge_tokens(query)[:128]
    if not terms or not index_ids:
        return []
    dialect = db.bind.dialect.name
    if dialect == "sqlite":
        expression = "bm25(yp_knowledge_fts)"
        source = "yp_knowledge_fts JOIN yp_knowledge_chunk c ON c.id = yp_knowledge_fts.chunk_id"
        predicate = "yp_knowledge_fts MATCH :query"
        query_value = " OR ".join(terms)
        order = "ASC"
    elif dialect == "postgresql":
        expression = "ts_rank_cd(to_tsvector('simple', c.search_terms), to_tsquery('simple', :query))"
        source = "yp_knowledge_chunk c"
        predicate = "to_tsvector('simple', c.search_terms) @@ to_tsquery('simple', :query)"
        query_value = " | ".join(terms)
        order = "DESC"
    else:
        expression = "MATCH(c.search_terms) AGAINST (:query IN BOOLEAN MODE)"
        source = "yp_knowledge_chunk c"
        predicate = f"{expression} > 0"
        query_value = " ".join(terms)
        order = "DESC"
    statement = text(
        f"SELECT c.id, {expression} AS score FROM {source} JOIN yp_knowledge_document d ON d.id = c.document_id "
        f"WHERE c.index_id IN :index_ids AND d.is_deleted = 0 AND {predicate} ORDER BY score {order}, c.id ASC LIMIT :limit",
    ).bindparams(bindparam("index_ids", expanding=True))
    rows = (
        await db.execute(
            statement,
            {
                "query": query_value,
                "index_ids": index_ids,
                "limit": limit,
            },
        )
    ).all()
    return [(int(row[0]), float(row[1])) for row in rows]


async def vector_candidates(
    db: AsyncSession,
    index_ids: list[int],
    vector: list[float],
    limit: int,
    threshold: float,
    backend: KnowledgeVectorBackend,
) -> list[tuple[int, float]]:
    """在授权过滤后计算有界精确余弦，PostgreSQL 可显式使用 pgvector 距离。"""
    if not index_ids:
        return []
    if backend == KnowledgeVectorBackend.PGVECTOR:
        if db.bind.dialect.name != "postgresql" or not await db.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")):
            raise ValueError("所选向量后端不可用，请配置 pgvector 或使用 exact")
        statement = text(
            "SELECT c.id, 1 - (CAST(CAST(c.embedding AS text) AS vector) <=> CAST(:vector AS vector)) AS score "
            "FROM yp_knowledge_chunk c JOIN yp_knowledge_document d ON d.id = c.document_id "
            "WHERE c.index_id IN :index_ids AND d.is_deleted = 0 AND c.embedding IS NOT NULL "
            "ORDER BY score DESC, c.id ASC LIMIT :limit",
        ).bindparams(bindparam("index_ids", expanding=True))
        import json

        rows = (
            await db.execute(
                statement,
                {
                    "index_ids": index_ids,
                    "vector": json.dumps(vector),
                    "limit": limit,
                },
            )
        ).all()
        return [(int(row[0]), float(row[1])) for row in rows if math.isfinite(row[1]) and row[1] >= threshold]
    rows = (
        await db.execute(
            select(KnowledgeChunk.id, KnowledgeChunk.embedding)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(KnowledgeChunk.index_id.in_(index_ids), KnowledgeDocument.is_deleted == 0, KnowledgeChunk.embedding.is_not(None))
            .limit(16001),
        )
    ).all()
    if len(rows) > 16000:
        raise ValueError("精确向量检索超过候选预算，请缩小范围或切换后端")
    ranked = []
    for identifier, embedding in rows:
        if len(embedding) != len(vector):
            raise ValueError("索引维度与查询不一致")
        score = sum(left * right for left, right in zip(embedding, vector, strict=True))
        if math.isfinite(score) and score >= threshold:
            ranked.append((identifier, score))
    return sorted(ranked, key=lambda item: (-item[1], item[0]))[:limit]
