# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:08:18
# @Author  : yangyuexiong
# @File    : knowledge_sources.py

"""为结构化文档统一生成有界规范原文、证据位置及解析摘要。"""

from app.core.enums import KnowledgeFailure
from app.services.knowledge_formats import ROUTED_PARSER_VERSION
from app.services.knowledge_parser import MAX_TEXT_CHARACTERS, KnowledgeProcessingError, ParsedKnowledge, _normalize

MAX_SOURCE_BYTES = 8192
MAX_SOURCE_NODES = 20000
MAX_SOURCE_DEPTH = 48


class SourceKnowledgeBuilder:
    """各格式只提交语义单元，统一校验原文偏移及资源边界。"""

    def __init__(self, format_name: str):
        """保存格式身份与即将发布的规范证据，不保存可执行内容。"""
        self.parts = []
        self.blocks = []
        self.offset = 0
        self.metadata = {
            "parser": ROUTED_PARSER_VERSION,
            "format": format_name,
        }

    def append(self, text: str, source: dict, *, heading: str | None = None, page: int | None = None) -> None:
        """原子保存一条规范证据，禁止截断配置值或表格记录冒充完整内容。"""
        text = _normalize(text)
        if not text:
            return
        if len(text.encode()) > MAX_SOURCE_BYTES or len(self.blocks) >= 10000:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        if self.parts:
            self.parts.append("\n")
            self.offset += 1
        start = self.offset
        self.offset += len(text)
        if self.offset > MAX_TEXT_CHARACTERS:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        self.parts.append(text)
        self.blocks.append(
            {
                "start": start,
                "end": self.offset,
                "page": page,
                "heading": heading[:512] if heading else None,
                "source_locator": source,
            },
        )

    def append_text(self, text: str, source: dict, *, heading: str | None = None, page: int | None = None) -> None:
        """长正文按字符预切成有界来源块，配置字段和表格记录使用原子追加。"""
        text = _normalize(text)
        for start in range(0, len(text), 2000):
            self.append(text[start : start + 2000], dict(source), heading=heading, page=page)

    def finish(self) -> ParsedKnowledge:
        """拒绝空知识，返回能按偏移精确切片的原文及完整摘要。"""
        if not self.blocks:
            raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
        self.metadata["block_count"] = len(self.blocks)
        return ParsedKnowledge("".join(self.parts), self.blocks, self.metadata)


def source_table_text(table: int, row: int, values: list[str], headers: list[str] | None = None) -> str:
    """仅在表格显式声明表头且列数一致时，将列名带入完整行证据。"""
    columns = []
    for index, value in enumerate(values, 1):
        label = f"列{index}"
        if headers is not None and len(headers) == len(values) and headers[index - 1]:
            label += f"（{headers[index - 1]}）"
        columns.append(f"{label}={value or '[空单元格]'}")
    return f"表{table}；行{row}；" + "；".join(columns)
