# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 14:23:24
# @Author  : yangyuexiong
# @File    : model_adapter.py

from decimal import Decimal
from types import EllipsisType

from langchain_anthropic import ChatAnthropic
from langchain_aws import ChatBedrockConverse
from langchain_core.exceptions import ContextOverflowError
from langchain_core.language_models.chat_models import BaseChatModel
from openai import BadRequestError
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import AzureChatOpenAI

from app.core.enums import ModelThinkingMode, ProviderKind
from app.core.custom_exception import CustomException
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.provider_credentials import decrypt_credentials
from app.services.model_stream import ReasoningChatOpenAI as ChatOpenAI


def thinking_request_options(provider_kind: str, thinking_mode: str | None = ModelThinkingMode.AUTO) -> dict:
    """将显式思考偏好转换为已支持的供应商扩展，默认不改变请求。"""
    mode = ModelThinkingMode(thinking_mode or ModelThinkingMode.AUTO)
    if mode == ModelThinkingMode.AUTO:
        return {}
    if provider_kind != ProviderKind.OPENAI_COMPATIBLE:
        raise CustomException(
            status_code=409,
            custom_code=10005,
            detail="显式思考模式仅支持实现 chat_template_kwargs.enable_thinking 的 OpenAI 兼容服务，请改为跟随模型或选择兼容模型",
        )
    return {
        "extra_body": {
            "chat_template_kwargs": {
                "enable_thinking": mode == ModelThinkingMode.ENABLED,
            },
        },
    }


def validate_output_token_setting(provider_kind: str, max_output_tokens: int | None) -> None:
    """拒绝对必须指定输出额度的供应商清空额度，避免 SDK 偷补默认值。"""
    if max_output_tokens is None and provider_kind == ProviderKind.ANTHROPIC:
        raise CustomException(
            status_code=409,
            custom_code=10005,
            detail="Anthropic Messages 要求指定最大输出 Token，请为该供应商设置正整数额度",
        )


def build_chat_model(
    model: ModelCatalog,
    provider: ProviderConnection,
    *,
    temperature: Decimal | None = None,
    max_output_tokens: int | None | EllipsisType = ...,
    thinking_mode: str | None = ModelThinkingMode.AUTO,
) -> BaseChatModel:
    """构造聊天模型；显式空额度不传给供应商，省略额度保留既有能力探测默认值。"""
    if model.provider_id != provider.id or model.status != 1 or provider.status != 1 or model.is_deleted != 0 or provider.is_deleted != 0:
        raise CustomException(status_code=409, custom_code=10005, detail="模型或供应商连接不可用")
    if max_output_tokens is ...:
        max_output_tokens = 1024 if provider.kind == ProviderKind.ANTHROPIC else None
    validate_output_token_setting(provider.kind, max_output_tokens)
    thinking_options = thinking_request_options(provider.kind, thinking_mode)
    api_key, headers = decrypt_credentials(provider.encrypted_credentials)
    generation = (
        {
            "temperature": float(temperature),
        }
        if temperature is not None
        else {}
    )
    token_limit = (
        {
            "max_tokens": max_output_tokens,
        }
        if max_output_tokens is not None
        else {}
    )
    if provider.kind == ProviderKind.AZURE_OPENAI:
        if not api_key or not provider.api_version:
            raise CustomException(status_code=400, custom_code=10005, detail="Azure OpenAI 缺少 API 密钥或版本")
        return AzureChatOpenAI(
            azure_endpoint=provider.base_url,
            azure_deployment=model.external_id,
            api_version=provider.api_version,
            api_key=api_key,
            default_headers=headers,
            timeout=provider.timeout_seconds,
            stream_chunk_timeout=provider.timeout_seconds,
            max_retries=0,
            stream_usage=True,
            **generation,
            **token_limit,
        )
    if provider.kind == ProviderKind.GEMINI:
        if not api_key:
            raise CustomException(status_code=400, custom_code=10005, detail="Gemini 缺少 API 密钥")
        return ChatGoogleGenerativeAI(
            model=model.external_id,
            api_key=api_key,
            base_url=provider.base_url,
            api_version=provider.api_version or "v1beta",
            additional_headers=headers,
            request_timeout=provider.timeout_seconds,
            retries=0,
            **generation,
            **(
                {
                    "max_output_tokens": max_output_tokens,
                }
                if max_output_tokens is not None
                else {}
            ),
        )
    if provider.kind == ProviderKind.BEDROCK:
        if not provider.region:
            raise CustomException(status_code=400, custom_code=10005, detail="Bedrock 缺少区域")
        return ChatBedrockConverse(
            model=model.external_id,
            region_name=provider.region,
            timeout=provider.timeout_seconds,
            max_retries=0,
            **generation,
            **token_limit,
        )
    if provider.kind == ProviderKind.ANTHROPIC:
        if not api_key:
            raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
        base_url = provider.base_url.removesuffix("/v1")
        default_headers = {
            **headers,
            **(
                {
                    "anthropic-version": provider.api_version,
                }
                if provider.api_version
                else {}
            ),
        }
        return ChatAnthropic(
            model=model.external_id,
            api_key=api_key,
            base_url=base_url,
            default_headers=default_headers,
            timeout=provider.timeout_seconds,
            max_retries=0,
            max_tokens=max_output_tokens,
            **generation,
        )
    if provider.kind in {ProviderKind.OPENAI, ProviderKind.OPENAI_COMPATIBLE, ProviderKind.OLLAMA}:
        if provider.kind == ProviderKind.OPENAI and not api_key:
            raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
        return ChatOpenAI(
            **thinking_options,
            model=model.external_id,
            base_url=provider.base_url,
            api_key=api_key or "not-required",
            default_headers=headers,
            timeout=provider.timeout_seconds,
            stream_chunk_timeout=provider.timeout_seconds,
            max_retries=0,
            stream_usage=True,
            **generation,
            **token_limit,
        )
    raise CustomException(status_code=400, custom_code=10005, detail="不支持的供应商类型")


def is_model_context_overflow(error: BaseException) -> bool:
    """仅识别 SDK 上下文异常或明确的兼容服务窗口拒绝，不暴露上游正文。"""
    current = error
    seen = set()
    while current is not None and id(current) not in seen and len(seen) < 8:
        seen.add(id(current))
        if isinstance(current, ContextOverflowError):
            return True
        if isinstance(current, BadRequestError):
            body = current.body
            if isinstance(body, dict):
                body = body.get("error", body)
            if isinstance(body, dict):
                code = body.get("code")
                message = body.get("message")
                if code == "context_length_exceeded":
                    return True
                if isinstance(message, str):
                    message = message.lower()
                    if "maximum context length" in message and "tokens" in message and "reduce" in message:
                        return True
        current = current.__cause__ or current.__context__
    return False
