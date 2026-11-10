# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:47:27
# @Author  : yangyuexiong
# @File    : knowledge_parser.py

"""在有界子进程中提取知识文档，并以规范原文偏移生成片段。"""

import asyncio
import io
import json
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass

from app.core.enums import KnowledgeFailure

MAX_TEXT_CHARACTERS = 500000
MAX_PARSER_BYTES = 8 * 1024 * 1024


class KnowledgeProcessingError(ValueError):
    """携带脱敏失败类别，原始文件和上游异常不写入日志。"""

    def __init__(self, code: KnowledgeFailure):
        """保存可持久化和安全对外展示的失败类别。"""
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True)
class ParsedKnowledge:
    """规范原文和可追溯页面标题块。"""

    text: str
    blocks: list[dict]


def _normalize(text: str) -> str:
    """统一换行和 Unicode，保留可直接切片核对的规范原文。"""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join((line.rstrip() for line in text.replace("\x00", "").splitlines())).strip()


def parse_knowledge(content: bytes, media_type: str) -> ParsedKnowledge:
    """按实际类型提取文字，扫描 PDF 明确返回需要 OCR。"""
    units = []
    if media_type in ("text/plain", "text/markdown"):
        units = [(content.decode("utf-8-sig"), None, None)]
    elif media_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 200:
            raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
        units = [(page.extract_text() or "", number, None) for number, page in enumerate(reader.pages, 1)]
        if any(not text.strip() for text, _, _ in units):
            raise KnowledgeProcessingError(KnowledgeFailure.OCR_REQUIRED)
    elif media_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        from docx import Document
        from docx.table import Table

        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if len(archive.infolist()) > 4096 or sum(item.file_size for item in archive.infolist()) > 32 * 1024 * 1024:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        document = Document(io.BytesIO(content))
        heading = None
        for block in document.iter_inner_content():
            if isinstance(block, Table):
                text = "\n".join(" | ".join(cell.text for cell in row.cells) for row in block.rows)
            else:
                text = block.text
                if block.style and block.style.name.startswith("Heading"):
                    heading = text[:512]
            units.append((text, None, heading))
    else:
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
    parts, blocks, offset, heading = [], [], 0, None
    for text, page, inherited_heading in units:
        for line in _normalize(text).splitlines():
            if not line.strip():
                continue
            if media_type == "text/markdown" and re.match(r"^#{1,6}\s+", line):
                heading = re.sub(r"^#{1,6}\s+", "", line)[:512]
            if inherited_heading:
                heading = inherited_heading
            if parts:
                parts.append("\n")
                offset += 1
            start = offset
            parts.append(line)
            offset += len(line)
            if offset > MAX_TEXT_CHARACTERS or len(blocks) >= 10000:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            blocks.append(
                {
                    "start": start,
                    "end": offset,
                    "page": page,
                    "heading": heading,
                },
            )
    if not blocks:
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
    return ParsedKnowledge("".join(parts), blocks)


def chunk_knowledge(parsed: ParsedKnowledge, size: int, overlap: int) -> list[dict]:
    """按标题和页码聚合结构块，超长段落有界重叠，所有片段保持精确偏移。"""
    if not 200 <= size <= 2000 or not 0 <= overlap < size // 2:
        raise ValueError("分块参数无效")
    groups = []
    for block in parsed.blocks:
        if groups and groups[-1]["page"] == block["page"] and groups[-1]["heading"] == block["heading"]:
            groups[-1]["end"] = block["end"]
        else:
            groups.append(dict(block))
    chunks = []
    for group in groups:
        start = group["start"]
        while start < group["end"]:
            end = min(start + size, group["end"])
            if end < group["end"]:
                split = parsed.text.rfind("\n", start + size // 2, end)
                if split > start:
                    end = split
            chunks.append(
                {
                    "position": len(chunks),
                    "content": parsed.text[start:end],
                    "start_offset": start,
                    "end_offset": end,
                    "page": group["page"],
                    "heading": group["heading"],
                },
            )
            if end == group["end"]:
                break
            start = max(start + 1, end - overlap)
    return chunks


async def parse_knowledge_isolated(content: bytes, media_type: str) -> ParsedKnowledge:
    """用可终止子进程隔离不可信解析，超时及取消时终止子进程。"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "app.services.knowledge_parser",
        media_type,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(content), timeout=25)
        if len(output) > MAX_PARSER_BYTES:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        payload = json.loads(output)
        if "failure" in payload:
            raise KnowledgeProcessingError(KnowledgeFailure(payload["failure"]))
        if process.returncode or not isinstance(payload.get("text"), str):
            raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
        return ParsedKnowledge(payload["text"], payload["blocks"])
    except (ValueError, KeyError, UnicodeError) as exc:
        if isinstance(exc, KnowledgeProcessingError):
            raise
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED) from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def main() -> None:
    """在子进程施加 CPU、文件描述符和虚拟内存约束，只输出受限结构。"""
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    try:
        content = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        parsed = parse_knowledge(content, sys.argv[1])
        payload = {
            "text": parsed.text,
            "blocks": parsed.blocks,
        }
        output = json.dumps(payload, ensure_ascii=False)
        if len(output.encode()) > MAX_PARSER_BYTES:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        print(output)
    except Exception as exc:
        code = exc.code if isinstance(exc, KnowledgeProcessingError) else KnowledgeFailure.PARSE_FAILED
        print(
            json.dumps(
                {
                    "failure": code.value,
                },
            ),
        )


if __name__ == "__main__":
    main()
