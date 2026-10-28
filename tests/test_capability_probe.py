# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 16:17:05
# @Author  : yangyuexiong
# @File    : test_capability_probe.py

import asyncio
import base64
import json
import os
import struct
import zlib
from decimal import Decimal

import httpx
import openai
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_google_genai.chat_models import GoogleAPIError, GoogleAuthenticationError, GoogleRateLimitError

from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.capability_probe import probe_capability
from app.services.model_probe import probe_model
from app.services.provider_probe import probe_provider


class FakeChatModel:
    """模拟文本、流式和工具调用的标准 LangChain 消息。"""

    def __init__(self, *, tool_calls: bool = True, stream_text: bool = True) -> None:
        """设置本次模拟是否返回工具调用或流式正文。"""
        self.tool_calls = tool_calls
        self.stream_text = stream_text
        self.bound = False

    def bind_tools(self, tools: list[dict]):
        """记录工具已绑定并返回可调用模型。"""
        assert tools[0]["function"]["name"] == "verify_capability"
        self.bound = True
        return self

    async def ainvoke(self, messages: list, **kwargs) -> AIMessage:
        """返回测试所需的文本或工具调用。"""
        assert kwargs["max_tokens"] == 128
        tool_calls = (
            [
                {
                    "name": "verify_capability",
                    "args": {
                        "value": "ping",
                    },
                    "id": "call-1",
                }
            ]
            if self.bound and self.tool_calls
            else []
        )
        return AIMessage(
            content="OK" if not self.bound else "",
            tool_calls=tool_calls,
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
            },
        )

    async def astream(self, messages: list, **kwargs):
        """返回流式正文及最终 Token 用量。"""
        assert kwargs["max_tokens"] == 128
        yield AIMessageChunk(content="O" if self.stream_text else "")
        yield AIMessageChunk(
            content="K" if self.stream_text else "",
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
            },
        )


class StatusChatModel(FakeChatModel):
    """按顺序返回网关或认证错误的模拟模型。"""

    def __init__(self, statuses: list[int]) -> None:
        """保存待模拟的上游状态码。"""
        super().__init__()
        self.statuses = statuses
        self.calls = 0

    async def ainvoke(self, messages: list, **kwargs) -> AIMessage:
        """依次抛出上游错误，状态码耗尽后返回正常消息。"""
        self.calls += 1
        if self.statuses:
            status = self.statuses.pop(0)
            response = httpx.Response(status, request=httpx.Request("POST", "https://models.example/v1/chat/completions"))
            raise openai.APIStatusError("secret-response", response=response, body=None)
        return await super().ainvoke(messages, **kwargs)


class BackendFailureChatModel(FakeChatModel):
    """按顺序模拟 Gemini 或 Bedrock SDK 的故障。"""

    def __init__(self, failures: list[Exception]) -> None:
        """记录下次调用应抛出的异常。"""
        super().__init__()
        self.failures = failures
        self.calls = 0

    async def ainvoke(self, messages: list, **kwargs) -> AIMessage:
        """抛出指定故障，耗尽后返回标准消息。"""
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return await super().ainvoke(messages, **kwargs)


class PartialStreamChatModel(FakeChatModel):
    """输出首个流式片段后模拟网关故障。"""

    def __init__(self) -> None:
        """初始化流式调用次数。"""
        super().__init__()
        self.calls = 0

    async def astream(self, messages: list, **kwargs):
        """首个片段已经到达时抛出上游错误。"""
        self.calls += 1
        yield AIMessageChunk(content="O")
        response = httpx.Response(502, request=httpx.Request("POST", "https://models.example/v1/chat/completions"))
        raise openai.APIStatusError("secret-response", response=response, body=None)


class MalformedStreamChatModel(FakeChatModel):
    """模拟上游流式事件让 SDK 收到非预期字段类型。"""

    async def astream(self, messages: list, **kwargs):
        """在迭代流式消息时抛出解析异常。"""
        raise AttributeError("upstream-event-secret")
        yield  # 保持异步迭代接口


class StructuredChatModel(FakeChatModel):
    """模拟结构化输出及解析失败。"""

    def __init__(self, value: str = "ping") -> None:
        """设置结构化返回的验证值。"""
        super().__init__()
        self.value = value

    def with_structured_output(self, schema: dict, *, method: str, include_raw: bool):
        """确认使用函数调用模式并返回模拟 Runnable。"""
        assert schema["properties"]["value"]["enum"] == ["ping"]
        assert method == "function_calling" and include_raw is True
        return self

    async def ainvoke(self, messages: list, **kwargs) -> dict:
        """返回原始消息、解析结果及可用 Token 用量。"""
        assert kwargs["max_tokens"] == 128
        raw = AIMessage(
            content="secret-response",
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
            },
        )
        return {
            "raw": raw,
            "parsed": {
                "value": self.value,
            },
            "parsing_error": None,
        }


class VisionChatModel(FakeChatModel):
    """检查视觉探测发送内嵌图片并模拟颜色识别。"""

    def __init__(self, answer: str = "red, green, blue, yellow") -> None:
        """设置模拟模型的颜色回答。"""
        super().__init__()
        self.answer = answer

    async def ainvoke(self, messages: list, **kwargs) -> AIMessage:
        """核对消息中的图片尺寸并返回模拟回答。"""
        assert kwargs["max_tokens"] == 512
        content = messages[0].content
        assert content[0]["type"] == "text" and content[1]["type"] == "image_url"
        image_url = content[1]["image_url"]["url"]
        assert image_url.startswith("data:image/png;base64,")
        image_bytes = base64.b64decode(image_url.partition(",")[2])
        assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", image_bytes[16:24]) == (64, 64)
        compressed_size = struct.unpack(">I", image_bytes[33:37])[0]
        assert image_bytes[37:41] == b"IDAT"
        pixels = zlib.decompress(image_bytes[41 : 41 + compressed_size])
        stride = 1 + 64 * 3
        assert [pixels[index : index + 3] for index in (1, 97, 32 * stride + 1, 32 * stride + 97)] == [
            b"\xff\x00\x00",
            b"\x00\xff\x00",
            b"\x00\x00\xff",
            b"\xff\xff\x00",
        ]
        return AIMessage(
            content=self.answer,
            usage_metadata={
                "input_tokens": 12,
                "output_tokens": 4,
                "total_tokens": 16,
            },
        )


async def exercise_capabilities() -> None:
    """验证逐项探测的成功、失败、用量与成本。"""
    model = ModelCatalog(
        id=1,
        provider_id=1,
        external_id="chat-1",
        display_name="Chat 1",
        input_price=Decimal("2.000000"),
        output_price=Decimal("4.000000"),
        price_currency="USD",
    )
    provider = ProviderConnection(id=1, timeout_seconds=5)
    for capability in ("text", "streaming", "tools"):
        result = await probe_capability(model, provider, capability, FakeChatModel())
        assert result.available is True and result.failure is None
        assert result.input_tokens == 12 and result.output_tokens == 4
        assert result.estimated_cost == Decimal("0.000040")
    assert (await probe_capability(model, provider, "tools", FakeChatModel(tool_calls=False))).failure == "invalid_response"
    assert (await probe_capability(model, provider, "streaming", FakeChatModel(stream_text=False))).failure == "invalid_response"
    malformed = await probe_capability(model, provider, "streaming", MalformedStreamChatModel())
    assert malformed.available is False and malformed.failure == "invalid_response"
    assert "upstream-event-secret" not in malformed.model_dump_json()
    structured = await probe_capability(model, provider, "structured_output", StructuredChatModel())
    assert structured.available is True and structured.input_tokens == 12 and structured.output_tokens == 4
    assert "secret-response" not in structured.model_dump_json()
    assert (await probe_capability(model, provider, "structured_output", StructuredChatModel("wrong"))).failure == "invalid_response"
    vision = await probe_capability(model, provider, "vision", VisionChatModel())
    assert vision.available is True and vision.input_tokens == 12 and vision.output_tokens == 4
    assert (await probe_capability(model, provider, "vision", VisionChatModel("yellow, blue, green, red"))).failure == "invalid_response"
    gateway = StatusChatModel([502])
    assert (await probe_capability(model, provider, "text", gateway)).available is True
    assert gateway.calls == 2
    authentication = StatusChatModel([401])
    assert (await probe_capability(model, provider, "text", authentication)).failure == "authentication"
    assert authentication.calls == 1
    rate_limit = StatusChatModel([429])
    assert (await probe_capability(model, provider, "text", rate_limit)).failure == "rate_limit"
    assert rate_limit.calls == 1
    repeated_gateway = StatusChatModel([503, 503, 503])
    assert (await probe_capability(model, provider, "text", repeated_gateway)).failure == "upstream"
    assert repeated_gateway.calls == 2
    partial_stream = PartialStreamChatModel()
    stream_failure = await probe_capability(model, provider, "streaming", partial_stream)
    assert stream_failure.failure == "upstream" and stream_failure.upstream_status_code == 502
    assert partial_stream.calls == 1 and "secret-response" not in stream_failure.model_dump_json()

    google_auth = BackendFailureChatModel([GoogleAuthenticationError("secret-response")])
    assert (await probe_capability(model, provider, "text", google_auth)).failure == "authentication"
    google_rate_limit = BackendFailureChatModel([GoogleRateLimitError("secret-response")])
    assert (await probe_capability(model, provider, "text", google_rate_limit)).failure == "rate_limit"
    assert google_rate_limit.calls == 1
    google_gateway = BackendFailureChatModel(
        [
            GoogleAPIError(
                503,
                {
                    "message": "secret-response",
                },
            )
        ]
    )
    assert (await probe_capability(model, provider, "text", google_gateway)).available is True
    assert google_gateway.calls == 2

    bedrock_auth = BackendFailureChatModel([NoCredentialsError()])
    assert (await probe_capability(model, provider, "text", bedrock_auth)).failure == "authentication"
    bedrock_gateway = BackendFailureChatModel(
        [
            ClientError(
                {
                    "Error": {
                        "Code": "ServiceUnavailableException",
                        "Message": "secret-response",
                    },
                    "ResponseMetadata": {
                        "HTTPStatusCode": 503,
                    },
                },
                "Converse",
            )
        ]
    )
    assert (await probe_capability(model, provider, "text", bedrock_gateway)).available is True
    assert bedrock_gateway.calls == 2


async def exercise_reasoning_summary(monkeypatch) -> None:
    """验证仅公开摘要可通过，隐藏内容与普通文本不能冒充摘要。"""
    from cryptography.fernet import Fernet

    from app.core.config import get_config
    from app.services.provider_credentials import encrypt_credentials

    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    model = ModelCatalog(
        id=1, provider_id=1, external_id="reasoning-model", input_price=Decimal("2"), output_price=Decimal("4"), price_currency="USD"
    )
    provider = ProviderConnection(
        id=1,
        kind="openai",
        base_url="https://api.openai.com/v1",
        timeout_seconds=5,
        encrypted_credentials=encrypt_credentials("probe-secret", {}),
    )
    requests: list[httpx.Request] = []

    def successful(request: httpx.Request) -> httpx.Response:
        """模拟 Responses API 返回公开摘要且保留请求供协议断言。"""
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "public summary",
                            }
                        ],
                        "content": [
                            {
                                "type": "reasoning_text",
                                "text": "hidden reasoning",
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(successful)) as client:
        result = await probe_capability(model, provider, "reasoning_summary", response_client=client)
    assert result.available is True and result.estimated_cost == Decimal("0.000040")
    assert "public summary" not in result.model_dump_json() and "hidden reasoning" not in result.model_dump_json()
    assert requests[0].url.path == "/v1/responses"
    assert requests[0].headers["authorization"] == "Bearer probe-secret"
    body = json.loads(requests[0].content)
    assert (
        body["reasoning"]
        == {
            "summary": "concise",
        }
        and body["store"] is False
    )

    def without_summary(request: httpx.Request) -> httpx.Response:
        """模拟只有隐藏推理内容或普通回答的响应。"""
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [],
                        "content": [
                            {
                                "type": "reasoning_text",
                                "text": "hidden reasoning",
                            }
                        ],
                    },
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "normal answer",
                            }
                        ],
                    },
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(without_summary)) as client:
        result = await probe_capability(model, provider, "reasoning_summary", response_client=client)
    assert result.failure == "invalid_response" and result.available is False

    calls = 0

    def retry_gateway(request: httpx.Request) -> httpx.Response:
        """验证未收到响应正文时网关错误至多重试一次。"""
        nonlocal calls
        calls += 1
        return httpx.Response(503) if calls == 1 else successful(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(retry_gateway)) as client:
        assert (await probe_capability(model, provider, "reasoning_summary", response_client=client)).available is True
    assert calls == 2

    def rate_limited(request: httpx.Request) -> httpx.Response:
        """模拟摘要接口限流，不读取上游错误正文。"""
        return httpx.Response(429, text="secret-response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(rate_limited)) as client:
        result = await probe_capability(model, provider, "reasoning_summary", response_client=client)
    assert result.failure == "rate_limit" and "secret-response" not in result.model_dump_json()

    provider.kind = "anthropic"
    assert (await probe_capability(model, provider, "reasoning_summary")).failure == "unsupported_capability"


def test_capability_probe() -> None:
    """运行逐项模型能力探测测试。"""
    asyncio.run(exercise_capabilities())


def test_reasoning_summary_probe(monkeypatch) -> None:
    """运行公开推理摘要的协议及脱敏回归测试。"""
    asyncio.run(exercise_reasoning_summary(monkeypatch))


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_OLLAMA_GEMMA4") != "1", reason="仅在显式启用本机 Ollama 验收时运行")
def test_live_ollama_gemma4() -> None:
    """使用本机 Gemma4 重复验证模型发现、五项能力及缺失模型分类。"""

    async def exercise_live_model() -> None:
        """运行不写入业务数据库的真实 Ollama 调用。"""
        provider = ProviderConnection(
            id=999001,
            kind="ollama",
            base_url="http://127.0.0.1:11434/v1",
            encrypted_credentials=None,
            timeout_seconds=45,
            status=1,
            is_deleted=0,
        )
        model = ModelCatalog(id=999001, provider_id=provider.id, external_id="gemma4:31b-cloud", status=1, is_deleted=0)
        discovered = await probe_provider(provider)
        assert discovered.available is True and model.external_id in discovered.models
        tested = await probe_model(model, provider)
        assert tested.available is True and tested.input_tokens is not None and tested.output_tokens is not None
        for capability in ("text", "streaming", "tools", "structured_output", "vision"):
            result = await probe_capability(model, provider, capability)
            assert result.available is True and result.input_tokens is not None and result.output_tokens is not None, capability
        missing = ModelCatalog(id=999002, provider_id=provider.id, external_id="missing-model-for-m2-probe", status=1, is_deleted=0)
        unavailable = await probe_model(missing, provider)
        assert unavailable.upstream_status_code == 404 and unavailable.failure == "model_not_found"
        summary = await probe_capability(model, provider, "reasoning_summary")
        assert summary.failure == "unsupported_capability"

    asyncio.run(exercise_live_model())
