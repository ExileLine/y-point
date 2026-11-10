# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:48:47
# @Author  : yangyuexiong
# @File    : embedding_adapter.py

"""独立于聊天模型的有界嵌入适配、响应校验与用量计量。"""

import asyncio
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, ModelCapability, ProviderKind
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.knowledge_parser import KnowledgeProcessingError
from app.services.provider_probe import provider_request_headers

MAX_EMBEDDING_DIMENSIONS = 4096
EMBEDDING_BATCH_SIZE = 16


@dataclass(frozen=True)
class EmbeddingResult:
    """已校验的单位向量与可核对的上游用量。"""

    vectors: list[list[float]]
    tokens: int | None
    cost: Decimal | None


async def load_embedding_model(db: AsyncSession, model_id: int, *, verified: bool = True) -> tuple[ModelCatalog, ProviderConnection]:
    """只接受启用的受支持供应商和独立探测通过的嵌入模型。"""
    model = await db.get(ModelCatalog, model_id)
    provider = await db.get(ProviderConnection, model.provider_id) if model is not None else None
    if (
        model is None
        or provider is None
        or model.is_deleted != 0
        or provider.is_deleted != 0
        or model.status != 1
        or provider.status != 1
        or provider.kind not in (ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE, ProviderKind.OLLAMA, ProviderKind.AZURE_OPENAI)
        or verified
        and (ModelCapability.EMBEDDING not in (model.verified_capabilities or []) or not model.embedding_dimensions)
    ):
        raise KnowledgeProcessingError(KnowledgeFailure.MODEL_UNAVAILABLE)
    return model, provider


def embedding_identity(model: ModelCatalog, provider: ProviderConnection) -> dict:
    """固定模型、维度及连接身份，凭据轮换不改变向量空间身份。"""
    return {
        "model_id": model.id,
        "external_id": model.external_id,
        "dimensions": model.embedding_dimensions,
        "provider_id": provider.id,
        "provider_kind": provider.kind,
        "base_url": provider.base_url,
        "api_version": provider.api_version,
    }


def normalize_vectors(raw: object, count: int, dimensions: int | None) -> list[list[float]]:
    """严格校验批量、固定维度、有限数值和非零范数，禁止静默截断向量。"""
    if not isinstance(raw, list) or len(raw) != count:
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
    result = []
    for vector in raw:
        if not isinstance(vector, list) or not 1 <= len(vector) <= MAX_EMBEDDING_DIMENSIONS:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
        if dimensions is None:
            dimensions = len(vector)
        if len(vector) != dimensions or any(type(value) not in (int, float) for value in vector):
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
        try:
            if any(not math.isfinite(value) for value in vector):
                raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
            norm = math.sqrt(sum(value * value for value in vector))
        except OverflowError:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING) from None
        if not math.isfinite(norm) or norm <= 0:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
        result.append([value / norm for value in vector])
    return result


async def embed_texts(
    model: ModelCatalog,
    provider: ProviderConnection,
    texts: list[str],
    *,
    dimensions: int | None,
    client: httpx.AsyncClient | None = None,
) -> EmbeddingResult:
    """批量输入有界且关闭隐式截断，网络及响应错误只公开脱敏类别。"""
    if not 1 <= len(texts) <= EMBEDDING_BATCH_SIZE or any(not value.strip() or len(value.encode()) > 8192 for value in texts):
        raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
    base = httpx.URL(provider.base_url)
    headers = {
        **provider_request_headers(provider),
        "Accept-Encoding": "identity",
    }
    if provider.kind == ProviderKind.OLLAMA:
        path = base.path.rstrip("/")
        if path.endswith("/v1"):
            path = path[:-3]
        url = base.copy_with(path=f"{path}/api/embed")
        payload = {
            "model": model.external_id,
            "input": texts,
            "truncate": False,
        }
    elif provider.kind == ProviderKind.AZURE_OPENAI:
        url = base.copy_with(path=f"{base.path.rstrip('/')}/openai/deployments/{quote(model.external_id, safe='')}/embeddings")
        url = url.copy_add_param("api-version", provider.api_version)
        payload = {
            "input": texts,
        }
    else:
        url = base.copy_with(path=f"{base.path.rstrip('/')}/embeddings")
        payload = {
            "model": model.external_id,
            "input": texts,
            "encoding_format": "float",
        }
    owned = client is None
    if owned:
        client = httpx.AsyncClient(
            timeout=min(provider.timeout_seconds, get_config().KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS), trust_env=False, follow_redirects=False
        )
    try:
        async with asyncio.timeout(min(provider.timeout_seconds, get_config().KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS)):
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    code = (
                        KnowledgeFailure.AUTHENTICATION
                        if response.status_code in (401, 403)
                        else KnowledgeFailure.RATE_LIMIT
                        if response.status_code == 429
                        else KnowledgeFailure.UPSTREAM
                    )
                    raise KnowledgeProcessingError(code)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
                output = bytearray()
                async for part in response.aiter_bytes():
                    output.extend(part)
                    if len(output) > 4 * 1024 * 1024:
                        raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        data = json.loads(output)
        if provider.kind == ProviderKind.OLLAMA:
            raw, tokens = data.get("embeddings"), data.get("prompt_eval_count")
        else:
            records = data.get("data")
            if not isinstance(records, list) or sorted(item.get("index", -1) for item in records) != list(range(len(texts))):
                raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
            raw = [item.get("embedding") for item in sorted(records, key=lambda item: item["index"])]
            tokens = data.get("usage", {}).get("prompt_tokens")
        vectors = normalize_vectors(raw, len(texts), dimensions)
        if tokens is not None and (type(tokens) is not int or tokens < 0):
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING)
        cost = Decimal(tokens) * model.input_price / Decimal(1000000) if tokens is not None and model.input_price is not None else None
        return EmbeddingResult(vectors, tokens, cost)
    except (httpx.TimeoutException, TimeoutError):
        raise KnowledgeProcessingError(KnowledgeFailure.TIMEOUT) from None
    except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError) as exc:
        if isinstance(exc, KnowledgeProcessingError):
            raise
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_EMBEDDING) from None
    finally:
        if owned:
            await client.aclose()
