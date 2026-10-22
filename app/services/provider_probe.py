# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:07
# @Author  : yangyuexiong
# @File    : provider_probe.py

import time

import httpx

from app.core.custom_exception import CustomException
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ConnectionTestOut
from app.services.provider_credentials import decrypt_credentials


def normalize_base_url(value: str) -> str:
    """规范化供应商根地址，只接受 HTTP 和 HTTPS 协议。"""
    try:
        url = httpx.URL(value.strip())
    except httpx.InvalidURL:
        raise CustomException(status_code=400, custom_code=10006, detail="供应商接口地址无效") from None
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password or url.query or url.fragment:
        raise CustomException(status_code=400, custom_code=10006, detail="供应商接口地址无效")
    return str(url).rstrip("/")


def validate_custom_headers(headers: dict[str, str]) -> None:
    """拒绝可覆盖认证、主机或传输行为的自定义请求头。"""
    forbidden = {"authorization", "x-api-key", "host", "content-length", "transfer-encoding", "anthropic-version"}
    allowed_name_symbols = "!#$%&'*+-.^_`|~"
    if len(headers) > 32:
        raise CustomException(status_code=400, custom_code=10006, detail="自定义请求头数量超过限制")
    for name, value in headers.items():
        valid_name = name and len(name) <= 128 and name.isascii()
        valid_name = valid_name and all(char.isalnum() or char in allowed_name_symbols for char in name)
        if not valid_name or len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise CustomException(status_code=400, custom_code=10006, detail="自定义请求头无效")
        try:
            httpx.Headers({name: value})
        except (ValueError, TypeError):
            raise CustomException(status_code=400, custom_code=10006, detail="自定义请求头无效") from None
        if name.lower() in forbidden:
            raise CustomException(status_code=400, custom_code=10006, detail="自定义请求头包含保留字段")


def provider_request_headers(provider: ProviderConnection) -> dict[str, str]:
    """解密供应商凭据并生成访问上游所需的请求头。"""
    api_key, custom_headers = decrypt_credentials(provider.encrypted_credentials)
    headers = {"accept": "application/json", **custom_headers}
    if provider.kind == "anthropic":
        if not api_key:
            raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
        headers.update({"x-api-key": api_key, "anthropic-version": provider.api_version or "2023-06-01"})
    elif api_key:
        headers["authorization"] = f"Bearer {api_key}"
    elif provider.kind == "openai":
        raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
    return headers


async def probe_provider(provider: ProviderConnection, client: httpx.AsyncClient | None = None) -> ConnectionTestOut:
    """调用供应商模型列表接口并返回模型 ID 和不含密钥的诊断结果。"""
    headers = provider_request_headers(provider)
    base_url = httpx.URL(provider.base_url)
    url = base_url.copy_with(path=f"{base_url.path.rstrip('/')}/models")
    started = time.perf_counter()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=provider.timeout_seconds, follow_redirects=False, trust_env=False)
    try:
        response = await client.get(url, headers=headers)
        latency_ms = round((time.perf_counter() - started) * 1000)
        if response.status_code in {401, 403}:
            return ConnectionTestOut(available=False, latency_ms=latency_ms, failure="authentication")
        if response.status_code == 429:
            return ConnectionTestOut(available=False, latency_ms=latency_ms, failure="rate_limit")
        if response.status_code >= 500:
            return ConnectionTestOut(available=False, latency_ms=latency_ms, failure="upstream")
        if response.status_code != 200:
            return ConnectionTestOut(available=False, latency_ms=latency_ms, failure="invalid_response")
        try:
            models = response.json()["data"]
            if not isinstance(models, list):
                raise ValueError("invalid models")
            model_ids = []
            model_names = {}
            for model in models:
                if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"].strip():
                    raise ValueError("invalid model id")
                model_id = model["id"]
                model_ids.append(model_id)
                display_name = model.get("display_name") or model.get("name")
                if isinstance(display_name, str) and display_name.strip() and display_name.isprintable():
                    model_names[model_id] = display_name.strip()
        except (ValueError, KeyError, TypeError):
            return ConnectionTestOut(available=False, latency_ms=latency_ms, failure="invalid_response")
        return ConnectionTestOut(
            available=True, latency_ms=latency_ms, model_count=len(model_ids), models=model_ids, model_names=model_names
        )
    except httpx.TimeoutException:
        return ConnectionTestOut(available=False, latency_ms=round((time.perf_counter() - started) * 1000), failure="timeout")
    except httpx.RequestError:
        return ConnectionTestOut(available=False, latency_ms=round((time.perf_counter() - started) * 1000), failure="network")
    finally:
        if own_client:
            await client.aclose()
