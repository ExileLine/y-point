# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 21:48:20
# @Author  : yangyuexiong
# @File    : model_stream.py

"""保留兼容供应商公开返回的思考片段，并按白名单提取展示文本。"""

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


class ReasoningChatOpenAI(ChatOpenAI):
    """补充兼容服务的公开思考字段，不转发签名、加密内容或未知扩展。"""

    def _convert_chunk_to_generation_chunk(self, chunk: dict, default_chunk_class: type, base_generation_info: dict | None) -> ChatGenerationChunk | None:
        """沿用 SDK 的正文、工具和用量解析，仅保留明确返回的思考文本。"""
        generation = super()._convert_chunk_to_generation_chunk(chunk, default_chunk_class, base_generation_info)
        choices = chunk.get("choices") or []
        if generation is not None and isinstance(generation.message, AIMessageChunk) and choices:
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return generation


def public_reasoning_text(message: AIMessage) -> str:
    """只读取模型显式公开的思考或摘要文本，不序列化原始响应及内部元数据。"""
    reasoning = message.additional_kwargs.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
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
