# -*- coding: utf-8 -*-
# @Time    : 2026-11-27 16:58:27
# @Author  : yangyuexiong
# @File    : test_openai_compatibility.py

"""仅用受控响应核实 Issue #9，不调用真实供应商。"""

import asyncio
import json

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from app.core.enums import ModelCapability, ModelProbeFailure, ProviderKind
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.model_probe import probe_model
from app.services.model_stream import ReasoningChatOpenAI, public_reasoning_text


def completion(reasoning_field="reasoning_content"):
    """生成非敏感的最小兼容响应。"""
    return {
        "id": "audit-response",
        "object": "chat.completion",
        "created": 1,
        "model": "audit-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "OK",
                    reasoning_field: "public test reasoning",
                },
            }
        ],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 2,
            "total_tokens": 4,
        },
    }


def response_handler(prefixed=False, reasoning_field="reasoning_content", content_type="application/json"):
    """通过本地 HTTP 传输回放 JSON 或带 data 前缀的单条消息。"""

    def handle(request):
        """返回受控的 HTTP 200 响应。"""
        body = json.dumps(completion(reasoning_field))
        return httpx.Response(
            200,
            text=("data: " if prefixed else "") + body,
            headers={
                "Content-Type": content_type,
            },
        )

    return handle


@pytest.mark.parametrize("prefixed", [False, True], ids=["standard-json", "data-prefixed-json"])
def test_text_probe_accepts_completion(prefixed):
    """通过正式文本测试入口核实相同有效内容的不同封装。"""

    async def exercise():
        """使用临时模型对象与受控 HTTP 客户端。"""
        model = ModelCatalog(id=1, external_id="audit-model", provider_id=1, status=1, is_deleted=0)
        provider = ProviderConnection(id=1, kind=ProviderKind.OPENAI_COMPATIBLE, base_url="https://audit.invalid/v1", status=1, is_deleted=0, timeout_seconds=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler(prefixed))) as client:
            result = await probe_model(model, provider, client)
        assert result.available, result.model_dump()

    asyncio.run(exercise())


@pytest.mark.parametrize("prefixed,content_type", [(False, "application/json"), (True, "application/json"), (True, "text/event-stream")])
def test_shared_sdk_nonstream_call(prefixed, content_type):
    """使用项目实际聊天适配类重放非流式调用。"""

    async def exercise():
        """将 SDK 网络请求全部转至受控传输。"""
        async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler(prefixed, content_type=content_type))) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", base_url="https://audit.invalid/v1", http_async_client=client, max_retries=0)
            message = await chat.ainvoke([HumanMessage(content="Reply OK")])
            assert message.text == "OK"

    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_streaming_reasoning_is_preserved(field):
    """确认流式 delta 的两种公开推理字段均能展示。"""
    chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", max_retries=0)
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": "",
                    field: "public test reasoning",
                },
                "finish_reason": None,
            }
        ],
    }
    generation = chat._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})
    assert public_reasoning_text(generation.message) == "public test reasoning"


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_extractor_accepts_public_reasoning_alias(field):
    """确认统一展示入口支持两种公开字段。"""
    message = AIMessage(
        content="OK",
        additional_kwargs={
            field: "public test reasoning",
        },
    )
    assert public_reasoning_text(message) == "public test reasoning"


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_nonstream_reasoning_is_preserved(field):
    """核实非流式 SDK 解析是否保留同一公开推理字段。"""

    async def exercise():
        """只替换上游响应，沿用正式 SDK 消息转换。"""
        async with httpx.AsyncClient(transport=httpx.MockTransport(response_handler(reasoning_field=field))) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", base_url="https://audit.invalid/v1", http_async_client=client, max_retries=0)
            message = await chat.ainvoke([HumanMessage(content="Reply OK")])
            assert public_reasoning_text(message) == "public test reasoning", message.additional_kwargs

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "changes, expected",
    [
        (
            {
                "name": "renamed",
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "remark": "new remark",
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "timeout_seconds": 30,
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "base_url": "https://new-audit.invalid/v1",
            },
            [],
        ),
        (
            {
                "base_url": "https://audit.invalid/v1",
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "base_url": "https://audit.invalid/v1/",
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "api_key": " test-key ",
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "custom_headers": {
                    "x-test-header": "test-value",
                },
            },
            [ModelCapability.TEXT],
        ),
        (
            {
                "api_key": "changed-key",
            },
            [],
        ),
        (
            {
                "custom_headers": {
                    "X-Test-Header": "changed-value",
                },
            },
            [],
        ),
        (
            {
                "api_key": None,
            },
            [],
        ),
        (
            {
                "custom_headers": {},
            },
            [],
        ),
    ],
)
@pytest.mark.parametrize("linked", [False, True])
def test_provider_update_preserves_unchanged_connection_verification(changes, expected, monkeypatch, linked):
    """用隔离数据库和正式路由函数核实哪些编辑会清空验证。"""
    import app.api.v1.providers as providers_api
    from cryptography.fernet import Fernet
    from app.core.config import get_config
    from app.services.provider_credentials import encrypt_credentials

    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.models import Admin
    from app.models.base import Base
    from app.schemas.provider import ProviderUpdateIn
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def exercise():
        """只创建内存 SQLite，不修改开发数据库。"""
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                actor = Admin(id=1, username="audit", password="unused", role="admin", status=1, is_deleted=0)
                provider = ProviderConnection(
                    id=1,
                    name="audit",
                    name_key="audit",
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="https://audit.invalid/v1",
                    creator_id=1,
                    status=1,
                    is_deleted=0,
                    timeout_seconds=15,
                )
                provider.encrypted_credentials = encrypt_credentials(
                    "test-key",
                    {
                        "X-Test-Header": "test-value",
                    },
                )
                provider.has_api_key = True
                provider.has_custom_headers = True
                model = ModelCatalog(
                    id=1,
                    external_id="audit-model",
                    display_name="Audit Model",
                    provider_id=1,
                    creator_id=1,
                    status=1,
                    is_deleted=0,
                    capabilities=[ModelCapability.TEXT],
                    verified_capabilities=[ModelCapability.TEXT],
                    capabilities_verified=True,
                )
                db.add_all([actor, provider, model])
                await db.commit()
                if linked:
                    from app.models.agent_config import AgentConfig

                    db.add(AgentConfig(id=1, owner_id=1, name="audit agent", default_model_id=1, is_deleted=0))
                    await db.commit()
                if linked and not expected:
                    from app.core.custom_exception import CustomException

                    with pytest.raises(CustomException) as caught:
                        await providers_api.update_provider(1, ProviderUpdateIn(**changes), actor, db)
                    assert caught.value.status_code == 409
                    await db.rollback()
                else:
                    await providers_api.update_provider(1, ProviderUpdateIn(**changes), actor, db)
                await db.refresh(model)
                assert model.verified_capabilities == ([ModelCapability.TEXT] if linked else expected)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_unsupported_summary_preserves_text_and_agent_selection():
    """核实兼容供应商拒绝 Responses 摘要后，文本仍可用且模型可选。"""
    import app.api.v1.models as models_api
    from app.models import Admin
    from app.models.base import Base
    from app.schemas.provider import CapabilityTestIn
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def exercise():
        """真实路由函数走摘要前置分支，不发起网络请求。"""
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                actor = Admin(id=1, username="audit", password="unused", role="admin", status=1, is_deleted=0)
                provider = ProviderConnection(
                    id=1,
                    name="audit",
                    name_key="audit",
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="https://audit.invalid/v1",
                    creator_id=1,
                    status=1,
                    is_deleted=0,
                )
                model = ModelCatalog(
                    id=1,
                    external_id="audit-model",
                    display_name="Audit Model",
                    provider_id=1,
                    creator_id=1,
                    status=1,
                    is_deleted=0,
                    capabilities=[ModelCapability.TEXT, ModelCapability.REASONING_SUMMARY],
                    verified_capabilities=[ModelCapability.TEXT],
                    capabilities_verified=False,
                )
                db.add_all([actor, provider, model])
                await db.commit()
                response = await models_api.test_model_capability(1, CapabilityTestIn(capability=ModelCapability.REASONING_SUMMARY), actor, db)
                result = json.loads(response.body)["data"]
                assert result["result"]["failure"] == ModelProbeFailure.UNSUPPORTED_CAPABILITY
                await db.refresh(model)
                assert model.verified_capabilities == [ModelCapability.TEXT] and not model.capabilities_verified
                available = json.loads((await models_api.list_available_models(actor, db)).body)["data"]
                assert [item["id"] for item in available] == [1] and available[0]["selectable"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("body", ["data: broken", 'data: {"choices":[{"delta":{"content":"O"}}]}', "data: {}\n\ndata: {}", "data: [DONE]"])
def test_nonstream_rejects_malformed_or_incremental_events(body):
    """非流式兼容层不能悄悄吞掉多段事件、损坏数据或只有增量的消息。"""

    async def exercise():
        """同时覆盖最小文本探测和共享 SDK 的拒绝行为。"""
        calls = []

        def handle(request):
            """记录请求次数，避免兼容回退造成重复计费。"""
            calls.append(request)
            return httpx.Response(
                200,
                text=body,
                headers={
                    "content-type": "application/json",
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_async_client=client, max_retries=0)
            with pytest.raises((ValueError, TypeError, KeyError)):
                await chat.ainvoke("OK")
            model = ModelCatalog(id=1, external_id="audit-model", provider_id=1)
            provider = ProviderConnection(id=1, kind=ProviderKind.OPENAI_COMPATIBLE, base_url="https://audit.invalid/v1", timeout_seconds=2)
            result = await probe_model(model, provider, client)
            assert not result.available and result.failure == ModelProbeFailure.INVALID_RESPONSE
        assert len(calls) == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("sync", [False, True])
def test_complete_event_preserves_tools_usage_and_reasoning(sync):
    """完整事件保留工具调用、用量和公开思考；同步与异步均只发起一次请求。"""
    calls = []

    def handle(request):
        """回放包含工具调用的单条完整消息及结束标记。"""
        calls.append(request)
        body = completion("reasoning")
        body["choices"][0]["message"].update(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-audit",
                        "type": "function",
                        "function": {
                            "name": "verify_capability",
                            "arguments": '{"value":"ping"}',
                        },
                    }
                ],
                "reasoning_signature": "private-signature",
                "encrypted_content": "private-encrypted",
            }
        )
        return httpx.Response(
            200,
            text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n",
            headers={
                "content-type": "application/json",
            },
        )

    def check(message):
        """核实兼容转换未改变工具参数、丢失用量或泄漏未知推理扩展。"""
        assert message.tool_calls[0]["args"] == {
            "value": "ping",
        }
        assert message.usage_metadata["total_tokens"] == 4
        assert public_reasoning_text(message) == "public test reasoning"
        assert "private-" not in str(message.additional_kwargs)
        assert len(calls) == 1

    async def exercise():
        """通过真实 SDK 异步链路验证单条兼容响应。"""
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_async_client=client, max_retries=0)
            check(await chat.ainvoke("OK"))

    if sync:
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_client=client, max_retries=0)
            check(chat.invoke("OK"))
    else:
        asyncio.run(exercise())


@pytest.mark.parametrize(
    "field_value,expected",
    [
        ("primary", "primary"),
        ("", "fallback"),
        (None, "fallback"),
        (
            {
                "encrypted": "private",
            },
            "fallback",
        ),
    ],
)
def test_reasoning_alias_precedence(field_value, expected):
    """标准字段优先，空值或非字符串才回退别名，避免重复拼接及扩展泄漏。"""
    message = AIMessage(
        content="OK",
        additional_kwargs={
            "reasoning_content": field_value,
            "reasoning": "fallback",
        },
    )
    assert public_reasoning_text(message) == expected


@pytest.mark.parametrize(
    "capability", [ModelCapability.TEXT, ModelCapability.STREAMING, ModelCapability.TOOLS, ModelCapability.STRUCTURED_OUTPUT, ModelCapability.VISION]
)
def test_probe_output_limit_is_inconclusive_without_retry(capability):
    """输出预算耗尽单独分类、保留用量且不自动追加付费请求。"""
    from app.services.capability_probe import probe_capability

    async def exercise():
        """真实 SDK 回放最终答案尚未生成的推理预算耗尽响应。"""
        calls = []

        def handle(request):
            """根据请求流式标记提供相同的截断终态。"""
            payload = json.loads(request.content)
            calls.append(payload)
            assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 1024
            body = completion()
            body["choices"][0]["message"]["content"] = None
            body["choices"][0]["finish_reason"] = "length"
            if payload.get("stream"):
                message = body["choices"][0].pop("message")
                body["choices"][0]["delta"] = message
                body["object"] = "chat.completion.chunk"
                return httpx.Response(
                    200,
                    text="data: " + json.dumps(body) + "\n\ndata: [DONE]\n",
                    headers={
                        "content-type": "text/event-stream",
                    },
                )
            return httpx.Response(200, json=body)

        model = ModelCatalog(id=1, external_id="audit-model", provider_id=1)
        provider = ProviderConnection(id=1, kind=ProviderKind.OPENAI_COMPATIBLE, base_url="https://audit.invalid/v1", timeout_seconds=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_async_client=client, max_retries=0, stream_usage=True)
            result = await probe_capability(model, provider, capability, chat_model=chat)
        assert not result.available and result.failure == ModelProbeFailure.OUTPUT_LIMIT
        assert result.output_tokens == 2
        assert len(calls) == 1
        assert "public test reasoning" not in result.model_dump_json()

    asyncio.run(exercise())


def test_reasoning_probe_budget_allows_final_text():
    """需要先生成推理的受控模型在新预算下能够产出最终正文。"""
    from app.services.capability_probe import probe_capability

    async def exercise():
        """通过真实 SDK 检查预算提升的实际请求与成功结果。"""

        def handle(request):
            """模拟至少需要 256 输出 Token 才生成最终答案的模型。"""
            payload = json.loads(request.content)
            assert payload.get("max_completion_tokens", payload.get("max_tokens")) >= 256
            return httpx.Response(200, json=completion())

        model = ModelCatalog(id=1, external_id="audit-model", provider_id=1)
        provider = ProviderConnection(id=1, kind=ProviderKind.OPENAI_COMPATIBLE, base_url="https://audit.invalid/v1", timeout_seconds=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_async_client=client, max_retries=0)
            result = await probe_capability(model, provider, ModelCapability.TEXT, chat_model=chat)
        assert result.available

    asyncio.run(exercise())


def test_responses_output_limit_retains_usage(monkeypatch):
    """Responses 明确报告预算耗尽时记录无法判定与用量，不误报不支持摘要。"""
    import app.services.capability_probe as probe_module

    def headers(provider):
        """隔离凭据配置，不改变真实 Responses 请求及响应解析。"""
        return {}

    monkeypatch.setattr(probe_module, "provider_request_headers", headers)
    from app.services.capability_probe import probe_capability

    async def exercise():
        """回放 Responses 的 incomplete 终态，避免真实上游费用。"""

        def handle(request):
            """确认统一有界预算并回放官方协议的截断原因。"""
            assert json.loads(request.content)["max_output_tokens"] == 1024
            return httpx.Response(
                200,
                json={
                    "status": "incomplete",
                    "incomplete_details": {
                        "reason": "max_output_tokens",
                    },
                    "output": [],
                    "usage": {
                        "input_tokens": 2,
                        "output_tokens": 1024,
                    },
                },
            )

        model = ModelCatalog(id=1, external_id="audit-model", provider_id=1)
        provider = ProviderConnection(id=1, kind=ProviderKind.OPENAI, base_url="https://audit.invalid/v1", timeout_seconds=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await probe_capability(model, provider, ModelCapability.REASONING_SUMMARY, response_client=client)
        assert result.failure == ModelProbeFailure.OUTPUT_LIMIT and result.output_tokens == 1024

    asyncio.run(exercise())


@pytest.mark.parametrize("capability", [ModelCapability.TOOLS, ModelCapability.STRUCTURED_OUTPUT])
def test_prefixed_completion_supports_capability_tools(capability):
    """兼容封装仍通过真实工具与结构化解析，结构化请求确实带有预算。"""
    from app.services.capability_probe import probe_capability

    async def exercise():
        """使用真实 SDK 的函数调用解析完成验证。"""
        calls = []

        def handle(request):
            """返回请求中声明的工具名称及固定参数。"""
            payload = json.loads(request.content)
            calls.append(payload)
            assert payload.get("max_completion_tokens", payload.get("max_tokens")) == 1024
            name = payload["tools"][0]["function"]["name"]
            body = completion()
            body["choices"][0]["finish_reason"] = "tool_calls"
            body["choices"][0]["message"] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-audit",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": '{"value":"ping"}',
                        },
                    }
                ],
            }
            return httpx.Response(
                200,
                text="data: " + json.dumps(body),
                headers={
                    "content-type": "application/json",
                },
            )

        model = ModelCatalog(id=1, external_id="audit-model", provider_id=1)
        provider = ProviderConnection(id=1, kind=ProviderKind.OPENAI_COMPATIBLE, base_url="https://audit.invalid/v1", timeout_seconds=2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_async_client=client, max_retries=0)
            result = await probe_capability(model, provider, capability, chat_model=chat)
        assert result.available and result.output_tokens == 2
        assert len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("verified", [False, True])
def test_inconclusive_probe_preserves_previous_capability_evidence(monkeypatch, verified):
    """无法判定不会抹掉既有验证，也不会把未验证能力标为成功。"""
    import app.api.v1.models as models_api
    from app.models import Admin
    from app.models.base import Base
    from app.models.model_call_record import ModelCallRecord
    from app.schemas.provider import CapabilityTestIn, ModelTestOut
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    async def truncated(model, provider, capability):
        """只替换付费调用，验证状态更新、记录与模型选择沿用正式路由。"""
        return ModelTestOut(model_id=model.id, external_id=model.external_id, available=False, latency_ms=1, failure=ModelProbeFailure.OUTPUT_LIMIT)

    monkeypatch.setattr(models_api, "probe_capability", truncated)

    async def exercise():
        """在隔离数据库中读取更新后的能力与历史调用记录。"""
        engine = create_async_engine("sqlite+aiosqlite://")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db:
                actor = Admin(id=1, username="audit", password="unused", role="admin", status=1, is_deleted=0)
                provider = ProviderConnection(
                    id=1,
                    name="audit",
                    name_key="audit",
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    base_url="https://audit.invalid/v1",
                    creator_id=1,
                    status=1,
                    is_deleted=0,
                )
                model = ModelCatalog(
                    id=1,
                    external_id="audit-model",
                    display_name="Audit",
                    provider_id=1,
                    creator_id=1,
                    status=1,
                    is_deleted=0,
                    capabilities=[ModelCapability.TEXT],
                    verified_capabilities=[ModelCapability.TEXT] if verified else [],
                    capabilities_verified=verified,
                )
                db.add_all([actor, provider, model])
                await db.commit()
                await models_api.test_model_capability(1, CapabilityTestIn(capability=ModelCapability.TEXT), actor, db)
                await db.refresh(model)
                assert model.verified_capabilities == ([ModelCapability.TEXT] if verified else [])
                assert model.capabilities_verified is verified
                record = await db.scalar(select(ModelCallRecord))
                assert not record.available and record.failure == ModelProbeFailure.OUTPUT_LIMIT
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("prefixed", [False, True])
@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
def test_native_structured_output_keeps_sdk_validation(prefixed, content_type):
    """原生结构化调用保留 SDK 类型解析；data 封装不会绕过其专用校验。"""
    from pydantic import BaseModel

    class StructuredAnswer(BaseModel):
        """模拟调用者需要的原生结构化类型。"""

        value: str

    async def exercise():
        """分别核实标准原生结果和非标准封装的拒绝边界。"""

        def handle(request):
            """回放完整结构化正文，避免发送真实模型请求。"""
            body = completion()
            body["choices"][0]["message"]["content"] = '{"value":"ping"}'
            return httpx.Response(
                200,
                text=("data: " if prefixed else "") + json.dumps(body),
                headers={
                    "content-type": content_type,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            chat = ReasoningChatOpenAI(model="audit-model", api_key="audit-only", http_async_client=client, max_retries=0)
            structured = chat.with_structured_output(StructuredAnswer, method="json_schema", include_raw=True)
            if prefixed:
                with pytest.raises((ValueError, AttributeError, TypeError)):
                    await structured.ainvoke("OK")
            else:
                result = await structured.ainvoke("OK")
                assert result["parsed"].value == "ping" and result["parsing_error"] is None

    asyncio.run(exercise())


def test_multiple_complete_events_are_rejected():
    """即使首条事件是完整且有效的回答，也不能截取它并丢弃后续事件。"""
    from app.services.model_stream import completion_response_json

    event = "data: " + json.dumps(completion())
    with pytest.raises(ValueError):
        completion_response_json(event + "\n\n" + event + "\n\ndata: [DONE]\n")
