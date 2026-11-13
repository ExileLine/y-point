# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:47:27
# @Author  : yangyuexiong
# @File    : knowledge_parser.py

"""在有界子进程中提取知识文档，并以规范原文偏移生成片段。"""

import asyncio
import io
import json
import os
import signal
import re
import sys
import tempfile
import unicodedata
import zipfile
from dataclasses import dataclass, field

from app.core.enums import KnowledgeContentRole, KnowledgeFailure, KnowledgeSourceKind

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
    metadata: dict = field(default_factory=dict)


def _normalize(text: str) -> str:
    """统一换行和 Unicode，保留可直接切片核对的规范原文。"""
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join((line.rstrip() for line in text.replace("\x00", "").splitlines())).strip()


def parse_knowledge(content: bytes, media_type: str, options: dict | None = None) -> ParsedKnowledge:
    """按实际类型提取文字和可追溯结构，PDF 独立选择文字层或本地 OCR。"""
    if media_type in ("text/csv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"):
        from app.services.knowledge_tables import parse_table_knowledge

        return parse_table_knowledge(content, media_type, options)
    if media_type == "text/html":
        from app.services.knowledge_html import parse_html_knowledge

        return parse_html_knowledge(content)
    if media_type in ("application/json", "application/yaml"):
        from app.services.knowledge_config import parse_configuration

        return parse_configuration(content, media_type)
    if media_type == "application/vnd.openxmlformats-officedocument.presentationml.presentation":
        from app.services.knowledge_slides import parse_pptx_knowledge

        return parse_pptx_knowledge(content)
    units = []
    if media_type in ("text/plain", "text/markdown"):
        units = [(content.decode("utf-8-sig"), None, None)]
    elif media_type == "application/pdf":
        from app.services.knowledge_pdf import parse_pdf_knowledge

        return parse_pdf_knowledge(content, options)
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
    if any(block.get("source_locator", {}).get("kind") == KnowledgeSourceKind.PDF for block in parsed.blocks):
        records = []
        for block in parsed.blocks:
            source = block["source_locator"]
            previous = records[-1] if records else None
            compatible = previous is not None and all(
                previous["source_locator"][key] == source[key] for key in ("page", "extraction_method", "layout", "table")
            )
            if compatible and block["end"] - previous["start"] <= size:
                previous["end"] = block["end"]
                box, added = previous["source_locator"]["bbox"], source["bbox"]
                previous["source_locator"]["bbox"] = [min(box[0], added[0]), min(box[1], added[1]), max(box[2], added[2]), max(box[3], added[3])]
            else:
                records.append(
                    {
                        **block,
                        "source_locator": dict(source),
                    },
                )
        return [
            {
                "position": position,
                "content": parsed.text[block["start"] : block["end"]],
                "start_offset": block["start"],
                "end_offset": block["end"],
                "page": block["page"],
                "heading": block["heading"],
                "source_locator": block["source_locator"],
            }
            for position, block in enumerate(records)
        ]
    if any(
        block.get("source_locator", {}).get("kind") in {KnowledgeSourceKind.HTML, KnowledgeSourceKind.CONFIG, KnowledgeSourceKind.SLIDE}
        for block in parsed.blocks
    ):
        chunks = []
        for block in parsed.blocks:
            source = block["source_locator"]
            atomic = source["kind"] == KnowledgeSourceKind.CONFIG or source.get("role") == KnowledgeContentRole.TABLE or source.get("table") is not None
            start = block["start"]
            while start < block["end"]:
                end = block["end"] if atomic else min(start + size, block["end"])
                chunks.append(
                    {
                        "position": len(chunks),
                        "content": parsed.text[start:end],
                        "start_offset": start,
                        "end_offset": end,
                        "page": block["page"],
                        "heading": block["heading"],
                        "source_locator": dict(source),
                    },
                )
                if end >= block["end"]:
                    break
                start = end - overlap
        return chunks
    if any(block.get("source_locator") for block in parsed.blocks):
        # 只合并同一表格的完整记录，超长记录不再按字符截断，避免字段失去归属。
        records = []
        for block in parsed.blocks:
            if records and records[-1]["heading"] == block["heading"] and block["end"] - records[-1]["start"] <= size:
                records[-1]["end"] = block["end"]
                locator = records[-1]["source_locator"]
                locator["row_end"] = block["source_locator"]["row_end"]
                columns = locator["columns"]
                locator["range"] = f"{columns[0]}{locator['row_start']}:{columns[-1]}{locator['row_end']}"
            else:
                records.append(
                    {
                        **block,
                        "source_locator": dict(block["source_locator"]),
                    }
                )
        return [
            {
                "position": position,
                "content": parsed.text[block["start"] : block["end"]],
                "start_offset": block["start"],
                "end_offset": block["end"],
                "page": block["page"],
                "heading": block["heading"],
                "source_locator": block["source_locator"],
            }
            for position, block in enumerate(records)
        ]
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


async def _run_parser_process(content: bytes, media_type: str, options: dict | None = None) -> bytes:
    """有界执行解析或 PDF 原页渲染，超时和取消同时终止 OCR 子进程组。"""
    with tempfile.TemporaryDirectory(prefix="y-point-parser-") as directory:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.services.knowledge_parser",
            media_type,
            json.dumps(options or {}, ensure_ascii=False),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
            env={
                **os.environ,
                "TMPDIR": directory,
            },
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(content), timeout=120 if media_type == "application/pdf" else 25)
            if len(output) > MAX_PARSER_BYTES:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            if process.returncode:
                raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
            return output
        except TimeoutError:
            raise KnowledgeProcessingError(KnowledgeFailure.TIMEOUT) from None
        finally:
            if process.returncode is None:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()


async def parse_knowledge_isolated(content: bytes, media_type: str, options: dict | None = None) -> ParsedKnowledge:
    """从受限解析子进程读取规范文本、来源块和质量摘要。"""
    try:
        payload = json.loads(await _run_parser_process(content, media_type, options))
        if "failure" in payload:
            raise KnowledgeProcessingError(KnowledgeFailure(payload["failure"]))
        if not isinstance(payload.get("text"), str):
            raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
        return ParsedKnowledge(payload["text"], payload["blocks"], payload.get("metadata", {}))
    except (ValueError, KeyError, UnicodeError) as exc:
        if isinstance(exc, KnowledgeProcessingError):
            raise
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED) from None


async def render_pdf_page_isolated(content: bytes, page: int) -> bytes:
    """在独立进程渲染单页，失败只返回脱敏错误，不混入图片响应。"""
    output = await _run_parser_process(
        content,
        "application/pdf",
        {
            "_render_page": page,
        },
    )
    if not output.startswith(b"\x89PNG\r\n\x1a\n"):
        try:
            failure = KnowledgeFailure(json.loads(output)["failure"])
        except (ValueError, KeyError, UnicodeError):
            failure = KnowledgeFailure.PARSE_FAILED
        raise KnowledgeProcessingError(failure)
    return output


def main() -> None:
    """在子进程施加 CPU、文件描述符和虚拟内存约束，只输出受限结构。"""
    import resource

    cpu = 90 if sys.argv[1] == "application/pdf" else 20
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024, 768 * 1024 * 1024))
    try:
        content = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
        if len(content) > 10 * 1024 * 1024:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        options = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
        if sys.argv[1] == "application/pdf" and "_render_page" in options:
            from app.services.knowledge_pdf import render_pdf_page

            sys.stdout.buffer.write(render_pdf_page(content, options["_render_page"]))
            return
        parsed = parse_knowledge(content, sys.argv[1], options)
        payload = {
            "text": parsed.text,
            "blocks": parsed.blocks,
            "metadata": parsed.metadata,
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
    # 使用规范模块身份，避免跨模块导入时重复定义异常类型而丢失失败类别。
    from app.services.knowledge_parser import main as parser_main

    parser_main()
