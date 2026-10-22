# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 16:14:42
# @Author  : yangyuexiong
# @File    : capability_probe.py

import asyncio
import re
import time
from decimal import Decimal

import anthropic
import httpx
import openai
from botocore.exceptions import BotoCoreError, ClientError, CredentialRetrievalError, NoCredentialsError, PartialCredentialsError
from langchain_google_genai.chat_models import (
    ChatGoogleGenerativeAIError,
    GoogleAPIError,
    GoogleAuthenticationError,
    GoogleInvalidRequestError,
    GoogleModelNotFoundError,
    GooglePermissionDeniedError,
    GoogleRateLimitError,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ModelTestOut, ProbeCapability
from app.services.model_adapter import build_chat_model
from app.services.bedrock_probe import _bedrock_failure
from app.services.probe_retry import PROBE_RETRY_DELAY_SECONDS, RETRYABLE_UPSTREAM_STATUSES, request_with_probe_retry
from app.services.provider_probe import provider_request_headers


VERIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "verify_capability",
        "description": "验证模型是否可以调用工具",
        "parameters": {
            "type": "object",
            "properties": {"value": {"type": "string", "description": "固定验证值"}},
            "required": ["value"],
        },
    },
}

STRUCTURED_SCHEMA = {
    "title": "VerifyStructuredOutput",
    "description": "返回固定验证值",
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["ping"], "description": "固定验证值", "examples": ["ping"]}},
    "required": ["value"],
    "additionalProperties": False,
}

# 固定的 64x64 红、绿、蓝、黄四象限 PNG，不依赖外部图片地址。
VISION_IMAGE_URL = "data:image/png;base64," + (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAVElEQVR42u3PsQkAMADDsPz/dHpDhwwtAu9GaTItfTwAAAAAAAAAAAAAAAAAAAAAAAAAAACADwHrQ5tp"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAADXHfsF7R5fMRs4AAAAAElFTkSuQmCC"
)
VISION_COLORS = ["red", "green", "blue", "yellow"]


def _failure_for_status(status: int) -> str:
    """将上游状态码转换为不含响应正文的故障类别。"""
    if status in {401, 403}:
        return "authentication"
    if status == 404:
        return "model_not_found"
    if status in {400, 422}:
        return "invalid_request"
    if status == 429:
        return "rate_limit"
    return "upstream" if status >= 500 else "invalid_response"


async def _probe_reasoning_summary(
    model: ModelCatalog, provider: ProviderConnection, client: httpx.AsyncClient | None = None,
) -> ModelTestOut:
    """请求 Responses API 的公开摘要字段，只记录存在性、用量与脱敏故障。"""
    started = time.perf_counter()
    if provider.kind != "openai":
        return ModelTestOut(
            model_id=model.id, external_id=model.external_id, available=False, latency_ms=0,
            failure="unsupported_capability",
        )
    headers = provider_request_headers(provider)
    base_url = httpx.URL(provider.base_url)
    url = base_url.copy_with(path=f"{base_url.path.rstrip('/')}/responses")
    payload = {
        "model": model.external_id,
        "input": "Reply with OK.",
        "reasoning": {"summary": "concise"},
        "max_output_tokens": 128,
        "store": False,
    }
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=provider.timeout_seconds, follow_redirects=False, trust_env=False)
    status = None
    failure = None
    input_tokens = None
    output_tokens = None
    try:
        response = await request_with_probe_retry(client, "POST", url, headers=headers, json_body=payload)
        status = response.status_code
        if status != 200:
            failure = _failure_for_status(status)
        else:
            data = response.json()
            if not isinstance(data, dict) or data.get("status") != "completed" or not isinstance(data.get("output"), list):
                failure = "invalid_response"
            else:
                has_summary = any(
                    isinstance(item, dict) and item.get("type") == "reasoning" and isinstance(item.get("summary"), list)
                    and any(isinstance(part, dict) and part.get("type") == "summary_text" and isinstance(part.get("text"), str)
                            and bool(part["text"].strip()) for part in item["summary"])
                    for item in data["output"]
                )
                if not has_summary:
                    failure = "invalid_response"
                usage = data.get("usage")
                if isinstance(usage, dict):
                    reported_input = usage.get("input_tokens")
                    reported_output = usage.get("output_tokens")
                    input_tokens = reported_input if type(reported_input) is int and reported_input >= 0 else None
                    output_tokens = reported_output if type(reported_output) is int and reported_output >= 0 else None
    except (ValueError, TypeError):
        failure = "invalid_response"
    except httpx.TimeoutException:
        failure = "timeout"
    except httpx.RequestError:
        failure = "network"
    finally:
        if own_client:
            await client.aclose()
    cost = None
    if input_tokens is not None and output_tokens is not None and model.input_price is not None and model.output_price is not None:
        cost = (Decimal(input_tokens) * model.input_price + Decimal(output_tokens) * model.output_price) / 1_000_000
    return ModelTestOut(
        model_id=model.id, external_id=model.external_id, available=failure is None,
        latency_ms=round((time.perf_counter() - started) * 1000), upstream_status_code=status,
        input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost=cost,
        price_currency=model.price_currency if cost is not None else None, failure=failure,
    )


async def probe_capability(
    model: ModelCatalog, provider: ProviderConnection, capability: ProbeCapability,
    chat_model: BaseChatModel | None = None,
    response_client: httpx.AsyncClient | None = None,
) -> ModelTestOut:
    """通过实际调用验证单项能力，不保留上游正文或推理摘要。"""
    if capability == "reasoning_summary":
        return await _probe_reasoning_summary(model, provider, response_client)
    chat_model = chat_model or build_chat_model(model, provider)
    started = time.perf_counter()
    for attempt in range(2):
        usage = None
        status = None
        failure = None
        saw_chunk = False
        try:
            async with asyncio.timeout(provider.timeout_seconds + 5):
                if capability == "streaming":
                    saw_text = False
                    async for chunk in chat_model.astream([HumanMessage(content="Reply with OK.")], max_tokens=128):
                        saw_chunk = True
                        saw_text = saw_text or bool(chunk.text.strip())
                        usage = chunk.usage_metadata or usage
                    if not saw_text:
                        failure = "invalid_response"
                elif capability == "tools":
                    bound = chat_model.bind_tools([VERIFY_TOOL])
                    message = await bound.ainvoke(
                        [HumanMessage(content="Call verify_capability with value ping. Do not answer in plain text.")],
                        max_tokens=128,
                    )
                    usage = message.usage_metadata
                    if not any(call["name"] == "verify_capability" and call["args"].get("value") == "ping" for call in message.tool_calls):
                        failure = "invalid_response"
                elif capability == "structured_output":
                    structured = chat_model.with_structured_output(STRUCTURED_SCHEMA, method="function_calling", include_raw=True)
                    response = await structured.ainvoke(
                        [HumanMessage(content="Return value ping in the requested structure.")], max_tokens=128,
                    )
                    raw = response.get("raw") if isinstance(response, dict) else None
                    usage = raw.usage_metadata if isinstance(raw, AIMessage) else None
                    parsed = response.get("parsed") if isinstance(response, dict) else None
                    if not isinstance(raw, AIMessage) or parsed != {"value": "ping"} or response.get("parsing_error") is not None:
                        failure = "invalid_response"
                elif capability == "vision":
                    prompt = "Name the colors in the image from top-left to bottom-right. Reply with English color names only."
                    message = await chat_model.ainvoke(
                        [HumanMessage(content=[
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": VISION_IMAGE_URL}},
                        ])],
                        max_tokens=512,
                    )
                    usage = message.usage_metadata
                    colors = re.findall(r"\b(red|green|blue|yellow)\b", message.text.lower())
                    if colors != VISION_COLORS:
                        failure = "invalid_response"
                else:
                    message = await chat_model.ainvoke([HumanMessage(content="Reply with OK.")], max_tokens=128)
                    usage = message.usage_metadata
                    if not message.text.strip():
                        failure = "invalid_response"
        except (TimeoutError, openai.APITimeoutError, anthropic.APITimeoutError, httpx.TimeoutException):
            failure = "timeout"
        except (openai.APIConnectionError, anthropic.APIConnectionError, httpx.RequestError):
            failure = "network"
        except (openai.APIStatusError, anthropic.APIStatusError) as exc:
            status = exc.status_code
            failure = _failure_for_status(status)
        except (GoogleAuthenticationError, GooglePermissionDeniedError):
            status, failure = 403, "authentication"
        except GoogleModelNotFoundError:
            status, failure = 404, "model_not_found"
        except GoogleInvalidRequestError:
            status, failure = 400, "invalid_request"
        except GoogleRateLimitError:
            status, failure = 429, "rate_limit"
        except GoogleAPIError as exc:
            status = exc.code
            failure = _failure_for_status(status)
        except ChatGoogleGenerativeAIError:
            failure = "invalid_response"
        except ClientError as exc:
            status, failure = _bedrock_failure(exc)
        except (NoCredentialsError, PartialCredentialsError, CredentialRetrievalError):
            failure = "authentication"
        except BotoCoreError as exc:
            failure = "timeout" if "Timeout" in type(exc).__name__ else "network"
        except (NotImplementedError, ValueError, TypeError, KeyError):
            failure = "invalid_response"
        if attempt == 0 and status in RETRYABLE_UPSTREAM_STATUSES and not saw_chunk:
            await asyncio.sleep(PROBE_RETRY_DELAY_SECONDS)
            continue
        break
    input_tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    output_tokens = usage.get("output_tokens") if isinstance(usage, dict) else None
    input_tokens = input_tokens if type(input_tokens) is int and input_tokens >= 0 else None
    output_tokens = output_tokens if type(output_tokens) is int and output_tokens >= 0 else None
    cost = None
    if input_tokens is not None and output_tokens is not None and model.input_price is not None and model.output_price is not None:
        cost = (Decimal(input_tokens) * model.input_price + Decimal(output_tokens) * model.output_price) / 1_000_000
    return ModelTestOut(
        model_id=model.id, external_id=model.external_id, available=failure is None,
        latency_ms=round((time.perf_counter() - started) * 1000), upstream_status_code=status,
        input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost=cost,
        price_currency=model.price_currency if cost is not None else None, failure=failure,
    )
