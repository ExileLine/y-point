# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:25:53
# @Author  : yangyuexiong
# @File    : test_provider_backends.py

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from cryptography.fernet import Fernet
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import AzureChatOpenAI

from app.core.config import get_config
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services import bedrock_probe
from app.services.model_adapter import build_chat_model
from app.services.model_probe import probe_model
from app.services.provider_credentials import encrypt_credentials
from app.services.provider_probe import probe_provider


async def exercise_http_backends() -> None:
    """验证 Azure 与 Gemini 的上游路径、认证、分页、用量和故障分类。"""
    azure = ProviderConnection(
        id=1,
        kind="azure_openai",
        base_url="https://example.openai.azure.com",
        api_version="2024-10-21",
        encrypted_credentials=encrypt_credentials("azure-secret", {}),
        timeout_seconds=5,
    )
    model = ModelCatalog(id=1, provider_id=1, external_id="chat-deployment", input_price=Decimal("2"), output_price=Decimal("4"))
    unavailable = await probe_provider(azure)
    assert unavailable.failure == "model_list_unavailable" and unavailable.models == []

    def azure_success(request: httpx.Request) -> httpx.Response:
        """确认 Azure 使用部署路径、版本参数和 api-key 请求头。"""
        assert request.url.path == "/openai/deployments/chat-deployment/chat/completions"
        assert request.url.params["api-version"] == "2024-10-21"
        assert request.headers["api-key"] == "azure-secret" and "authorization" not in request.headers
        assert "model" not in json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "OK",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 3,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(azure_success)) as client:
        result = await probe_model(model, azure, client)
    assert result.available is True and result.input_tokens == 8 and result.output_tokens == 3
    assert result.estimated_cost == Decimal("0.000028")

    def azure_denied(request: httpx.Request) -> httpx.Response:
        """模拟 Azure 拒绝密钥并检查原始错误不会返回。"""
        return httpx.Response(401, text="azure-secret must stay private")

    async with httpx.AsyncClient(transport=httpx.MockTransport(azure_denied)) as client:
        denied = await probe_model(model, azure, client)
    assert denied.failure == "authentication" and "azure-secret" not in denied.model_dump_json()

    gemini = ProviderConnection(
        id=2,
        kind="gemini",
        base_url="https://generativelanguage.googleapis.com",
        api_version="v1beta",
        encrypted_credentials=encrypt_credentials("gemini-secret", {}),
        timeout_seconds=5,
    )
    model.provider_id = 2
    model.external_id = "gemini-2.5-flash-lite"

    def gemini_models(request: httpx.Request) -> httpx.Response:
        """模拟 Gemini 的多页模型列表并过滤非文本生成模型。"""
        assert request.url.path == "/v1beta/models"
        assert request.headers["x-goog-api-key"] == "gemini-secret"
        if request.url.params.get("pageToken") == "page-2":
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash-lite",
                            "displayName": "Gemini Flash Lite",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/embedding-001",
                        "supportedGenerationMethods": ["embedContent"],
                    }
                ],
                "nextPageToken": "page-2",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gemini_models)) as client:
        discovered = await probe_provider(gemini, client)
    assert discovered.available is True and discovered.models == ["gemini-2.5-flash-lite"]
    assert discovered.model_names == {
        "gemini-2.5-flash-lite": "Gemini Flash Lite",
    }

    def gemini_success(request: httpx.Request) -> httpx.Response:
        """确认 Gemini 使用 generateContent 协议并解析候选和用量。"""
        assert request.url.path == "/v1beta/models/gemini-2.5-flash-lite:generateContent"
        assert request.headers["x-goog-api-key"] == "gemini-secret"
        assert json.loads(request.content)["contents"][0]["parts"][0]["text"] == "Hi"
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": "OK",
                                }
                            ],
                        },
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 3,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gemini_success)) as client:
        result = await probe_model(model, gemini, client)
    assert result.available is True and result.estimated_cost == Decimal("0.000028")

    def no_text(request: httpx.Request) -> httpx.Response:
        """拒绝 Gemini 仅返回候选壳而没有文本的响应。"""
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [],
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_text)) as client:
        assert (await probe_model(model, gemini, client)).failure == "invalid_response"

    def gemini_rate_limited(request: httpx.Request) -> httpx.Response:
        """模拟 Gemini 限流并确认不会重发请求。"""
        return httpx.Response(429, text="gemini-secret must stay private")

    async with httpx.AsyncClient(transport=httpx.MockTransport(gemini_rate_limited)) as client:
        limited = await probe_model(model, gemini, client)
    assert limited.failure == "rate_limit" and "gemini-secret" not in limited.model_dump_json()


class FakeBedrockClient:
    """提供 Bedrock 控制面和 Converse 的确定性响应。"""

    def __init__(self) -> None:
        """记录调用次数以验证有限重试。"""
        self.calls = 0

    def list_foundation_models(self, **kwargs) -> dict:
        """返回一个可发现的文本模型。"""
        assert kwargs == {
            "byOutputModality": "TEXT",
        }
        return {
            "ResponseMetadata": {
                "HTTPStatusCode": 200,
            },
            "modelSummaries": [
                {
                    "modelId": "amazon.nova-micro-v1:0",
                    "modelName": "Nova Micro",
                }
            ],
        }

    def converse(self, **kwargs) -> dict:
        """返回最小文本与标准 Bedrock 用量。"""
        self.calls += 1
        assert kwargs["modelId"] == "amazon.nova-micro-v1:0"
        assert kwargs["messages"][0]["content"] == [
            {
                "text": "Hi",
            }
        ]
        return {
            "ResponseMetadata": {
                "HTTPStatusCode": 200,
            },
            "output": {
                "message": {
                    "content": [
                        {
                            "text": "OK",
                        }
                    ],
                },
            },
            "usage": {
                "inputTokens": 8,
                "outputTokens": 3,
            },
        }


async def exercise_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 Bedrock 的发现、Converse、凭据失败及网关重试。"""
    provider = ProviderConnection(id=3, kind="bedrock", region="us-east-1", timeout_seconds=5)
    model = ModelCatalog(id=3, provider_id=3, external_id="amazon.nova-micro-v1:0", input_price=Decimal("2"), output_price=Decimal("4"))
    fake = FakeBedrockClient()
    monkeypatch.setattr(bedrock_probe, "_bedrock_client", lambda provider, service: fake)
    discovered = await probe_provider(provider)
    assert discovered.available is True and discovered.models == [model.external_id]
    assert discovered.model_names[model.external_id] == "Nova Micro"
    result = await probe_model(model, provider)
    assert result.available is True and result.input_tokens == 8 and result.output_tokens == 3
    assert result.estimated_cost == Decimal("0.000028")

    def missing_credentials(provider: ProviderConnection, service: str):
        """模拟当前环境未提供 AWS 身份。"""
        raise NoCredentialsError()

    monkeypatch.setattr(bedrock_probe, "_bedrock_client", missing_credentials)
    assert (await probe_provider(provider)).failure == "authentication"
    assert (await probe_model(model, provider)).failure == "authentication"

    def invalid_control_request(**kwargs) -> dict:
        """模拟控制面无效请求并确认连接诊断返回安全故障类别。"""
        raise ClientError(
            {
                "Error": {
                    "Code": "ValidationException",
                    "Message": "secret",
                },
                "ResponseMetadata": {
                    "HTTPStatusCode": 400,
                },
            },
            "ListFoundationModels",
        )

    fake.list_foundation_models = invalid_control_request
    monkeypatch.setattr(bedrock_probe, "_bedrock_client", lambda provider, service: fake)
    assert (await probe_provider(provider)).failure == "invalid_response"

    def gateway_failure(**kwargs) -> dict:
        """首次返回服务错误，第二次执行正常 Converse。"""
        if fake.calls == 1:
            fake.calls += 1
            raise ClientError(
                {
                    "Error": {
                        "Code": "ServiceUnavailableException",
                        "Message": "secret",
                    },
                    "ResponseMetadata": {
                        "HTTPStatusCode": 503,
                    },
                },
                "Converse",
            )
        return FakeBedrockClient.converse(fake, **kwargs)

    fake.converse = gateway_failure
    monkeypatch.setattr(bedrock_probe, "_bedrock_client", lambda provider, service: fake)
    retried = await probe_model(model, provider)
    assert retried.available is True and retried.failure is None
    assert fake.calls == 3 and "secret" not in retried.model_dump_json()


def test_http_provider_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行 Azure 与 Gemini 协议测试。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    asyncio.run(exercise_http_backends())


def test_bedrock_provider_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行 Bedrock SDK 协议测试。"""
    asyncio.run(exercise_bedrock(monkeypatch))


def test_provider_runtime_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证三类供应商的 LangChain 运行时配置。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    model = ModelCatalog(id=1, provider_id=1, external_id="chat-deployment", status=1, is_deleted=0)
    provider = ProviderConnection(
        id=1,
        kind="azure_openai",
        base_url="https://example.openai.azure.com",
        api_version="2024-10-21",
        encrypted_credentials=encrypt_credentials("azure-secret", {}),
        timeout_seconds=5,
        status=1,
        is_deleted=0,
    )
    azure = build_chat_model(model, provider)
    assert isinstance(azure, AzureChatOpenAI)
    assert azure.deployment_name == "chat-deployment" and azure.max_retries == 0
    configured_azure = build_chat_model(model, provider, temperature=Decimal("0.25"), max_output_tokens=2048)
    assert configured_azure.temperature == 0.25 and configured_azure.max_tokens == 2048

    provider.kind = "gemini"
    provider.base_url = "https://generativelanguage.googleapis.com"
    provider.api_version = "v1beta"
    provider.encrypted_credentials = encrypt_credentials("gemini-secret", {})
    model.external_id = "gemini-2.5-flash-lite"
    gemini = build_chat_model(model, provider)
    assert isinstance(gemini, ChatGoogleGenerativeAI)
    assert gemini.model == "gemini-2.5-flash-lite" and gemini.max_retries == 0
    configured_gemini = build_chat_model(model, provider, temperature=Decimal("0.25"), max_output_tokens=2048)
    assert configured_gemini.temperature == 0.25 and configured_gemini.max_output_tokens == 2048

    calls: list[dict] = []

    def fake_bedrock_model(**kwargs):
        """捕获 Bedrock 运行时配置而不访问 AWS。"""
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("app.services.model_adapter.ChatBedrockConverse", fake_bedrock_model)
    provider.kind = "bedrock"
    provider.region = "us-east-1"
    provider.encrypted_credentials = None
    model.external_id = "amazon.nova-micro-v1:0"
    build_chat_model(model, provider)
    assert calls == [
        {
            "model": "amazon.nova-micro-v1:0",
            "region_name": "us-east-1",
            "timeout": 5,
            "max_retries": 0,
        }
    ]
    build_chat_model(model, provider, temperature=Decimal("0.25"), max_output_tokens=2048)
    assert calls[-1]["temperature"] == 0.25 and calls[-1]["max_tokens"] == 2048
