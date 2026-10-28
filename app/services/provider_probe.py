# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:07
# @Author  : yangyuexiong
# @File    : provider_probe.py

import time
import re

import httpx

from app.core.custom_exception import CustomException
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ConnectionTestOut
from app.services.probe_retry import request_with_probe_retry
from app.services.provider_credentials import decrypt_credentials


def normalize_base_url(value: str | None, *, kind: str | None = None, region: str | None = None) -> str:
    """规范化供应商根地址，并为可省略地址的供应商生成默认值。"""
    if kind == "bedrock":
        if value is not None:
            raise CustomException(status_code=400, custom_code=10006, detail="Bedrock 接口地址由区域决定")
        if not region or not re.fullmatch(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", region):
            raise CustomException(status_code=400, custom_code=10006, detail="Bedrock 区域无效")
        suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
        return f"https://bedrock-runtime.{region}.{suffix}"
    value = value or ("https://generativelanguage.googleapis.com" if kind == "gemini" else None)
    if value is None:
        raise CustomException(status_code=400, custom_code=10006, detail="供应商接口地址不能为空")
    try:
        url = httpx.URL(value.strip())
    except httpx.InvalidURL:
        raise CustomException(status_code=400, custom_code=10006, detail="供应商接口地址无效") from None
    if url.scheme not in {"http", "https"} or not url.host or url.username or url.password or url.query or url.fragment:
        raise CustomException(status_code=400, custom_code=10006, detail="供应商接口地址无效")
    if kind in {"gemini", "azure_openai"} and url.path not in {"", "/"}:
        raise CustomException(status_code=400, custom_code=10006, detail="该供应商接口地址只允许填写服务根地址")
    return str(url).rstrip("/")


def validate_custom_headers(headers: dict[str, str]) -> None:
    """拒绝可覆盖认证、主机或传输行为的自定义请求头。"""
    forbidden = {
        "authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "host",
        "content-length",
        "transfer-encoding",
        "anthropic-version",
    }
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
    elif provider.kind == "azure_openai":
        if not api_key:
            raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
        headers["api-key"] = api_key
    elif provider.kind == "gemini":
        if not api_key:
            raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
        headers["x-goog-api-key"] = api_key
    elif api_key:
        headers["authorization"] = f"Bearer {api_key}"
    elif provider.kind == "openai":
        raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
    return headers


async def probe_provider(provider: ProviderConnection, client: httpx.AsyncClient | None = None) -> ConnectionTestOut:
    """调用供应商模型列表接口并返回模型 ID 和不含密钥的诊断结果。"""
    if provider.kind == "azure_openai":
        return ConnectionTestOut(available=False, latency_ms=0, failure="model_list_unavailable")
    if provider.kind == "bedrock":
        from app.services.bedrock_probe import probe_bedrock_provider

        return await probe_bedrock_provider(provider)
    headers = provider_request_headers(provider)
    base_url = httpx.URL(provider.base_url)
    path = f"/{provider.api_version or 'v1beta'}/models" if provider.kind == "gemini" else f"{base_url.path.rstrip('/')}/models"
    url = base_url.copy_with(path=path)
    started = time.perf_counter()
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=provider.timeout_seconds, follow_redirects=False, trust_env=False)
    try:
        response = await request_with_probe_retry(client, "GET", url, headers=headers)
        latency_ms = round((time.perf_counter() - started) * 1000)
        upstream_status_code = response.status_code
        failure = None
        if response.status_code in {401, 403}:
            failure = "authentication"
        elif response.status_code == 429:
            failure = "rate_limit"
        elif response.status_code in {404, 405, 501}:
            failure = "model_list_unavailable"
        elif response.status_code >= 500:
            failure = "upstream"
        elif response.status_code != 200:
            failure = "invalid_response"
        if failure is not None:
            return ConnectionTestOut(available=False, latency_ms=latency_ms, upstream_status_code=upstream_status_code, failure=failure)
        try:
            model_ids = []
            model_names = {}
            page_tokens: set[str] = set()
            for _ in range(20):
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError("invalid models")
                models = data["models"] if provider.kind == "gemini" else data["data"]
                if not isinstance(models, list):
                    raise ValueError("invalid models")
                for model in models:
                    if not isinstance(model, dict):
                        raise ValueError("invalid model id")
                    raw_id = model.get("name") if provider.kind == "gemini" else model.get("id")
                    if not isinstance(raw_id, str) or not raw_id.strip():
                        raise ValueError("invalid model id")
                    model_id = raw_id.removeprefix("models/") if provider.kind == "gemini" else raw_id
                    if provider.kind == "gemini":
                        methods = model.get("supportedGenerationMethods")
                        if not isinstance(methods, list):
                            raise ValueError("invalid generation methods")
                        if "generateContent" not in methods:
                            continue
                    model_ids.append(model_id)
                    display_name = model.get("displayName") if provider.kind == "gemini" else model.get("display_name") or model.get("name")
                    if isinstance(display_name, str) and display_name.strip() and display_name.isprintable():
                        model_names[model_id] = display_name.strip()
                token = data.get("nextPageToken") if provider.kind == "gemini" else None
                if not token:
                    break
                if not isinstance(token, str) or token in page_tokens:
                    raise ValueError("invalid page token")
                page_tokens.add(token)
                response = await request_with_probe_retry(client, "GET", url.copy_add_param("pageToken", token), headers=headers)
                if response.status_code != 200:
                    return ConnectionTestOut(
                        available=False,
                        latency_ms=round((time.perf_counter() - started) * 1000),
                        upstream_status_code=response.status_code,
                        failure="authentication"
                        if response.status_code in {401, 403}
                        else "rate_limit"
                        if response.status_code == 429
                        else "upstream"
                        if response.status_code >= 500
                        else "invalid_response",
                    )
            else:
                raise ValueError("too many model pages")
        except (ValueError, KeyError, TypeError):
            return ConnectionTestOut(
                available=False, latency_ms=latency_ms, upstream_status_code=upstream_status_code, failure="invalid_response"
            )
        return ConnectionTestOut(
            available=True,
            latency_ms=round((time.perf_counter() - started) * 1000),
            upstream_status_code=upstream_status_code,
            model_count=len(model_ids),
            models=model_ids,
            model_names=model_names,
        )
    except httpx.TimeoutException:
        return ConnectionTestOut(available=False, latency_ms=round((time.perf_counter() - started) * 1000), failure="timeout")
    except httpx.RequestError:
        return ConnectionTestOut(available=False, latency_ms=round((time.perf_counter() - started) * 1000), failure="network")
    finally:
        if own_client:
            await client.aclose()
