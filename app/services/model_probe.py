# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 11:19:04
# @Author  : yangyuexiong
# @File    : model_probe.py

import time

import httpx

from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ModelTestOut
from app.services.provider_probe import provider_request_headers


async def probe_model(model: ModelCatalog, provider: ProviderConnection, client: httpx.AsyncClient | None = None) -> ModelTestOut:
    """发送最小文本生成请求，诊断指定模型是否可以实际调用。"""
    headers = provider_request_headers(provider)
    base_url = httpx.URL(provider.base_url)
    if provider.kind == "anthropic":
        endpoint = "messages"
        payload = {"model": model.external_id, "max_tokens": 1, "messages": [{"role": "user", "content": "Hi"}]}
    else:
        endpoint = "chat/completions"
        token_field = "max_completion_tokens" if provider.kind == "openai" else "max_tokens"
        payload = {"model": model.external_id, token_field: 1, "messages": [{"role": "user", "content": "Hi"}], "stream": False}
    url = base_url.copy_with(path=f"{base_url.path.rstrip('/')}/{endpoint}")
    started = time.perf_counter()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=provider.timeout_seconds, follow_redirects=False, trust_env=False)
    try:
        response = await client.post(url, headers=headers, json=payload)
        latency_ms = round((time.perf_counter() - started) * 1000)
        status = response.status_code
        failure = None
        if status in {401, 403}:
            failure = "authentication"
        elif status == 404:
            failure = "model_not_found"
        elif status in {400, 422}:
            failure = "invalid_request"
        elif status == 429:
            failure = "rate_limit"
        elif status >= 500:
            failure = "upstream"
        elif status != 200:
            failure = "invalid_response"
        if failure is None:
            try:
                data = response.json()
                key = "content" if provider.kind == "anthropic" else "choices"
                if not isinstance(data, dict) or not isinstance(data.get(key), list) or not data[key]:
                    failure = "invalid_response"
                elif provider.kind == "anthropic" and not isinstance(data[key][0], dict):
                    failure = "invalid_response"
                elif provider.kind != "anthropic" and (
                    not isinstance(data[key][0], dict) or not isinstance(data[key][0].get("message"), dict)
                ):
                    failure = "invalid_response"
            except (ValueError, TypeError):
                failure = "invalid_response"
        return ModelTestOut(
            model_id=model.id, external_id=model.external_id, available=failure is None, latency_ms=latency_ms,
            upstream_status_code=status, failure=failure,
        )
    except httpx.TimeoutException:
        return ModelTestOut(
            model_id=model.id, external_id=model.external_id, available=False,
            latency_ms=round((time.perf_counter() - started) * 1000), failure="timeout",
        )
    except httpx.RequestError:
        return ModelTestOut(
            model_id=model.id, external_id=model.external_id, available=False,
            latency_ms=round((time.perf_counter() - started) * 1000), failure="network",
        )
    finally:
        if own_client:
            await client.aclose()
