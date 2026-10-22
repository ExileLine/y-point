# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 11:19:04
# @Author  : yangyuexiong
# @File    : model_probe.py

import time
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ModelTestOut
from app.services.probe_retry import request_with_probe_retry
from app.services.provider_probe import provider_request_headers


async def probe_model(model: ModelCatalog, provider: ProviderConnection, client: httpx.AsyncClient | None = None) -> ModelTestOut:
    """发送最小文本生成请求，诊断指定模型是否可以实际调用。"""
    if provider.kind == "bedrock":
        from app.services.bedrock_probe import probe_bedrock_model

        return await probe_bedrock_model(model, provider)
    headers = provider_request_headers(provider)
    base_url = httpx.URL(provider.base_url)
    if provider.kind == "anthropic":
        endpoint = "messages"
        payload = {"model": model.external_id, "max_tokens": 1, "messages": [{"role": "user", "content": "Hi"}]}
    elif provider.kind == "azure_openai":
        endpoint = f"openai/deployments/{quote(model.external_id, safe='')}/chat/completions"
        payload = {"messages": [{"role": "user", "content": "Hi"}], "max_tokens": 16, "stream": False}
    elif provider.kind == "gemini":
        endpoint = f"{provider.api_version or 'v1beta'}/models/{quote(model.external_id, safe='')}:generateContent"
        payload = {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}], "generationConfig": {"maxOutputTokens": 64}}
    else:
        endpoint = "chat/completions"
        token_field = "max_completion_tokens" if provider.kind == "openai" else "max_tokens"
        payload = {"model": model.external_id, token_field: 1, "messages": [{"role": "user", "content": "Hi"}], "stream": False}
    url = base_url.copy_with(path=f"{base_url.path.rstrip('/')}/{endpoint}")
    if provider.kind == "azure_openai":
        url = url.copy_add_param("api-version", provider.api_version)
    started = time.perf_counter()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=provider.timeout_seconds, follow_redirects=False, trust_env=False)
    try:
        response = await request_with_probe_retry(client, "POST", url, headers=headers, json_body=payload)
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
        input_tokens = None
        output_tokens = None
        estimated_cost = None
        if failure is None:
            try:
                data = response.json()
                key = "content" if provider.kind == "anthropic" else "candidates" if provider.kind == "gemini" else "choices"
                if not isinstance(data, dict) or not isinstance(data.get(key), list) or not data[key]:
                    failure = "invalid_response"
                elif provider.kind == "anthropic" and not isinstance(data[key][0], dict):
                    failure = "invalid_response"
                elif provider.kind == "gemini" and (
                    not isinstance(data[key][0], dict) or not isinstance(data[key][0].get("content"), dict)
                    or not isinstance(data[key][0]["content"].get("parts"), list)
                    or not any(isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"].strip()
                               for part in data[key][0]["content"]["parts"])
                ):
                    failure = "invalid_response"
                elif provider.kind not in {"anthropic", "gemini"} and (
                    not isinstance(data[key][0], dict) or not isinstance(data[key][0].get("message"), dict)
                ):
                    failure = "invalid_response"
                usage = data.get("usageMetadata") if provider.kind == "gemini" else data.get("usage")
                if failure is None and isinstance(usage, dict):
                    input_key, output_key = {
                        "gemini": ("promptTokenCount", "candidatesTokenCount"),
                        "anthropic": ("input_tokens", "output_tokens"),
                    }.get(provider.kind, ("prompt_tokens", "completion_tokens"))
                    reported_input = usage.get(input_key)
                    reported_output = usage.get(output_key)
                    if type(reported_input) is int and reported_input >= 0:
                        input_tokens = reported_input
                    if type(reported_output) is int and reported_output >= 0:
                        output_tokens = reported_output
                    if (
                        input_tokens is not None
                        and output_tokens is not None
                        and model.input_price is not None
                        and model.output_price is not None
                    ):
                        estimated_cost = (
                            Decimal(input_tokens) * model.input_price + Decimal(output_tokens) * model.output_price
                        ) / 1_000_000
            except (ValueError, TypeError):
                failure = "invalid_response"
        return ModelTestOut(
            model_id=model.id, external_id=model.external_id, available=failure is None, latency_ms=latency_ms,
            upstream_status_code=status, input_tokens=input_tokens, output_tokens=output_tokens,
            estimated_cost=estimated_cost, price_currency=model.price_currency if estimated_cost is not None else None,
            failure=failure,
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
