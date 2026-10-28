# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:25:01
# @Author  : yangyuexiong
# @File    : bedrock_probe.py

import asyncio
import time
from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, CredentialRetrievalError, NoCredentialsError, PartialCredentialsError

from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ConnectionTestOut, ModelTestOut
from app.services.probe_retry import PROBE_RETRY_DELAY_SECONDS, RETRYABLE_UPSTREAM_STATUSES


def _bedrock_client(provider: ProviderConnection, service: str) -> Any:
    """使用进程的 AWS 凭据链构造指定区域的无自动重试客户端。"""
    config = Config(connect_timeout=provider.timeout_seconds, read_timeout=provider.timeout_seconds, retries={"max_attempts": 0})
    return boto3.client(service, region_name=provider.region, config=config)


def _bedrock_failure(exc: ClientError) -> tuple[int | None, str]:
    """将 AWS 错误码映射为不暴露上游正文的故障类别。"""
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = exc.response.get("Error", {}).get("Code")
    if code in {"AccessDeniedException", "UnrecognizedClientException", "InvalidSignatureException", "ExpiredTokenException"}:
        return status, "authentication"
    if code in {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}:
        return status, "rate_limit"
    if code == "ResourceNotFoundException":
        return status, "model_not_found"
    if code in {"ValidationException", "BadRequestException"}:
        return status, "invalid_request"
    if status in {401, 403}:
        return status, "authentication"
    if status == 429:
        return status, "rate_limit"
    return status, "upstream" if isinstance(status, int) and status >= 500 else "invalid_response"


async def probe_bedrock_provider(provider: ProviderConnection) -> ConnectionTestOut:
    """读取区域内支持文本的基础模型，返回模型 ID 和脱敏故障。"""
    started = time.perf_counter()
    status = None
    failure = None
    model_ids: list[str] = []
    model_names: dict[str, str] = {}
    try:
        client = await asyncio.to_thread(_bedrock_client, provider, "bedrock")
        for attempt in range(2):
            try:
                response = await asyncio.to_thread(client.list_foundation_models, byOutputModality="TEXT")
                failure = None
                break
            except ClientError as exc:
                status, failure = _bedrock_failure(exc)
                if attempt == 0 and status in RETRYABLE_UPSTREAM_STATUSES:
                    await asyncio.sleep(PROBE_RETRY_DELAY_SECONDS)
                    continue
                raise
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        models = response.get("modelSummaries")
        if not isinstance(models, list):
            failure = "invalid_response"
        else:
            for item in models:
                model_id = item.get("modelId") if isinstance(item, dict) else None
                if not isinstance(model_id, str) or not model_id.strip():
                    failure = "invalid_response"
                    break
                model_ids.append(model_id)
                name = item.get("modelName")
                if isinstance(name, str) and name.strip() and name.isprintable():
                    model_names[model_id] = name.strip()
    except ClientError as exc:
        status, failure = _bedrock_failure(exc)
    except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError):
        failure = "authentication"
    except BotoCoreError as exc:
        failure = "timeout" if "Timeout" in type(exc).__name__ else "network"
    if failure in {"model_not_found", "invalid_request"}:
        failure = "invalid_response"
    latency_ms = round((time.perf_counter() - started) * 1000)
    if failure:
        return ConnectionTestOut(available=False, latency_ms=latency_ms, upstream_status_code=status, failure=failure)
    return ConnectionTestOut(
        available=True,
        latency_ms=latency_ms,
        upstream_status_code=status,
        model_count=len(model_ids),
        models=model_ids,
        model_names=model_names,
    )


async def probe_bedrock_model(model: ModelCatalog, provider: ProviderConnection) -> ModelTestOut:
    """使用 Bedrock Converse 发送最小文本请求并汇总安全诊断信息。"""
    started = time.perf_counter()
    status = None
    failure = None
    input_tokens = None
    output_tokens = None
    try:
        client = await asyncio.to_thread(_bedrock_client, provider, "bedrock-runtime")
        for attempt in range(2):
            try:
                response = await asyncio.to_thread(
                    client.converse,
                    modelId=model.external_id,
                    messages=[{"role": "user", "content": [{"text": "Hi"}]}],
                    inferenceConfig={"maxTokens": 64},
                )
                failure = None
                break
            except ClientError as exc:
                status, failure = _bedrock_failure(exc)
                if attempt == 0 and status in RETRYABLE_UPSTREAM_STATUSES:
                    await asyncio.sleep(PROBE_RETRY_DELAY_SECONDS)
                    continue
                raise
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        content = response.get("output", {}).get("message", {}).get("content")
        if not isinstance(content, list) or not any(
            isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip() for part in content
        ):
            failure = "invalid_response"
        usage = response.get("usage")
        if isinstance(usage, dict):
            reported_input = usage.get("inputTokens")
            reported_output = usage.get("outputTokens")
            input_tokens = reported_input if type(reported_input) is int and reported_input >= 0 else None
            output_tokens = reported_output if type(reported_output) is int and reported_output >= 0 else None
    except ClientError as exc:
        status, failure = _bedrock_failure(exc)
    except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError):
        failure = "authentication"
    except BotoCoreError as exc:
        failure = "timeout" if "Timeout" in type(exc).__name__ else "network"
    cost = None
    if input_tokens is not None and output_tokens is not None and model.input_price is not None and model.output_price is not None:
        cost = (Decimal(input_tokens) * model.input_price + Decimal(output_tokens) * model.output_price) / 1_000_000
    return ModelTestOut(
        model_id=model.id,
        external_id=model.external_id,
        available=failure is None,
        latency_ms=round((time.perf_counter() - started) * 1000),
        upstream_status_code=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=cost,
        price_currency=model.price_currency if cost is not None else None,
        failure=failure,
    )
