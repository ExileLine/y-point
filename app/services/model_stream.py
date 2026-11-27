# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 21:48:20
# @Author  : yangyuexiong
# @File    : model_stream.py

"""保留兼容供应商公开返回的思考片段，并按白名单提取展示文本。"""

import json

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI


def completion_response_json(body: str) -> dict:
    """解析标准 JSON 或单条完整 data 事件，拒绝多段增量与未知封装。"""
    text = body.strip()
    if text.startswith("data:"):
        lines = text.splitlines()
        if lines and lines[-1].strip() == "data: [DONE]":
            lines.pop()
        text = "\n".join(lines).strip()[5:].strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("模型响应必须为 JSON 对象")
    if body.strip().startswith("data:"):
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not all(isinstance(item, dict) and isinstance(item.get("message"), dict) for item in choices):
            raise ValueError("非流式兼容响应必须包含完整消息")
    return data


def _public_reasoning(fields: dict) -> str:
    """优先读取标准公开字段，再回退兼容别名；忽略非字符串值。"""
    for name in ("reasoning_content", "reasoning"):
        value = fields.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


class ReasoningChatOpenAI(ChatOpenAI):
    """补充兼容服务的公开思考字段，不转发签名、加密内容或未知扩展。"""

    def _fallback_chat_result(self, error: json.JSONDecodeError, messages: list, stop: list[str] | None, kwargs: dict) -> ChatResult:
        """只复用本次完整响应，不重发请求，也不绕过原生结构化或 Responses 解析。"""
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        if self._use_responses_api(payload) or "response_format" in payload or not error.doc.strip().startswith("data:"):
            raise error
        result = self._create_chat_result(completion_response_json(error.doc), getattr(error, "generation_info", None))
        response = getattr(error, "response", None)
        if self.include_response_headers and response is not None:
            for generation in result.generations:
                generation.generation_info = generation.generation_info or {}
                generation.generation_info["headers"] = dict(response.headers)
        return result

    async def _agenerate(self, messages: list, stop: list[str] | None = None, run_manager=None, **kwargs) -> ChatResult:
        """异步调用仅在 SDK 无法解析 data 封装时执行本地兼容转换。"""
        try:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except json.JSONDecodeError as error:
            return self._fallback_chat_result(error, messages, stop, kwargs)

    def _generate(self, messages: list, stop: list[str] | None = None, run_manager=None, **kwargs) -> ChatResult:
        """同步调用沿用相同的单响应兼容边界。"""
        try:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except json.JSONDecodeError as error:
            return self._fallback_chat_result(error, messages, stop, kwargs)

    def _create_chat_result(self, response, generation_info: dict | None = None) -> ChatResult:
        """保留 SDK 正文、工具与用量转换，并补回非流式公开思考字段。"""
        if isinstance(response, str) and response.strip().startswith("data:"):
            error = json.JSONDecodeError("非流式响应使用 data 封装", response, 0)
            error.generation_info = generation_info
            raise error
        result = super()._create_chat_result(response, generation_info)
        choices = (response.get("choices") if isinstance(response, dict) else getattr(response, "choices", None)) or []
        for generation, choice in zip(result.generations, choices):
            message = choice.get("message") if isinstance(choice, dict) else choice.message
            fields = (
                message
                if isinstance(message, dict)
                else {
                    "reasoning_content": getattr(message, "reasoning_content", None),
                    "reasoning": getattr(message, "reasoning", None),
                }
            )
            reasoning = _public_reasoning(fields or {})
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None) -> ChatGenerationChunk | None:
        """沿用 SDK 的正文、工具和用量解析，仅保留明确返回的思考文本。"""
        generation = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or []
        if generation is not None and isinstance(generation.message, AIMessageChunk) and choices:
            delta = choices[0].get("delta") or {}
            reasoning = _public_reasoning(delta)
            if reasoning:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation


def public_reasoning_text(message: AIMessage) -> str:
    """只读取模型显式公开的思考或摘要文本，不序列化原始响应及内部元数据。"""
    reasoning = _public_reasoning(message.additional_kwargs)
    if reasoning:
        return reasoning
    if isinstance(message.content, list):
        thinking = "".join(
            block["thinking"]
            for block in message.content
            if isinstance(block, dict) and block.get("type") == "thinking" and isinstance(block.get("thinking"), str)
        )
        if thinking:
            return thinking
    parts = []
    for block in message.content_blocks:
        if block.get("type") == "reasoning" and isinstance(block.get("reasoning"), str):
            parts.append(block["reasoning"])
    return "".join(parts)
