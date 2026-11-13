# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 22:12:57
# @Author  : yangyuexiong
# @File    : knowledge_pdf.py

"""按页提取 PDF 布局和本地 OCR 证据，保留坐标、质量和原文边界。"""

import csv
import io
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, KnowledgePdfExtraction, KnowledgePdfLayout, KnowledgePdfMode, KnowledgePdfPageKind, KnowledgeSourceKind
from app.services.knowledge_parser import MAX_TEXT_CHARACTERS, KnowledgeProcessingError, ParsedKnowledge, _normalize

PDF_PARSER_VERSION = "pdf-layout-ocr-v2"
MAX_PDF_PAGES = 200
MAX_PDF_PIXELS = 12000000
MAX_OCR_BYTES = 8 * 1024 * 1024


def pdf_runtime_configuration() -> dict:
    """固定会影响证据和质量的配置，使重试与解析缓存沿用同一处理参数。"""
    config = get_config()
    return {
        "version": PDF_PARSER_VERSION,
        "ocr_enabled": config.KNOWLEDGE_PDF_OCR_ENABLED,
        "ocr_command": config.KNOWLEDGE_PDF_OCR_COMMAND,
        "languages": config.KNOWLEDGE_PDF_OCR_LANGUAGES,
        "min_confidence": config.KNOWLEDGE_PDF_OCR_MIN_CONFIDENCE,
        "max_ocr_pages": config.KNOWLEDGE_PDF_OCR_MAX_PAGES,
        "dpi": config.KNOWLEDGE_PDF_OCR_DPI,
    }


def _normalize_pdf_line(text: str) -> str:
    """合并同一提取行内汉字之间的分词空格，保留英文、数字及跨行边界。"""
    return re.sub(r"(?<=[\u3400-\u9fff])[ \t]+(?=[\u3400-\u9fff])", "", _normalize(text))


def _bbox(words: list[dict]) -> list[float]:
    """计算 PDF 点坐标包围框，坐标原点为页面左上角。"""
    return [
        round(min(word["x0"] for word in words), 2),
        round(min(word["top"] for word in words), 2),
        round(max(word["x1"] for word in words), 2),
        round(max(word["bottom"] for word in words), 2),
    ]


def _native_lines(words: list[dict], center: float) -> list[tuple[str, list[float]]]:
    """按列和行组织文字，存在稳定中缝时先读左列再读右列。"""
    left = [word for word in words if word["x1"] < center - 8]
    right = [word for word in words if word["x0"] > center + 8]
    crossing = len(words) - len(left) - len(right)
    columns = [left, right] if len(left) >= 3 and len(right) >= 3 and crossing == 0 else [words]
    result = []
    for column in columns:
        lines = []
        for word in sorted(column, key=lambda item: (round(item["top"] / 3), item["x0"])):
            if lines and abs(lines[-1][0]["top"] - word["top"]) <= 3:
                lines[-1].append(word)
            else:
                lines.append([word])
        for line in lines:
            ordered = sorted(line, key=lambda item: item["x0"])
            text = _normalize(" ".join(word["text"] for word in ordered))
            if text:
                result.append((text, _bbox(ordered)))
    return result


def _render_page(page, dpi: int) -> bytes:
    """渲染单页到有界 PNG，超大页面不缩放绕过预算。"""
    import pypdfium2

    output = io.BytesIO()
    page.pdf.stream.seek(0)
    with pypdfium2.PdfDocument(page.pdf.stream) as document:
        original = document.get_page(page.page_number - 1)
        try:
            # 统一渲染完整 MediaBox，避免 CropBox 裁剪导致原页和证据坐标偏移。
            original.set_cropbox(*original.get_mediabox())
            width, height = original.get_size()
            pixels = math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72)
            if pixels > MAX_PDF_PIXELS:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            bitmap = original.render(scale=dpi / 72)
            try:
                image = bitmap.to_pil()
                try:
                    image.save(output, format="PNG")
                finally:
                    image.close()
            finally:
                bitmap.close()
        finally:
            original.close()
    if output.tell() > MAX_OCR_BYTES:
        raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
    return output.getvalue()


def _ocr_page(page, runtime: dict) -> tuple[list[tuple[str, list[float]]], float]:
    """调用本地 Tesseract TSV，按字符加权质量拒绝不可靠结果，不删除低分词。"""
    command = shutil.which(runtime["ocr_command"]) if runtime["ocr_enabled"] else None
    if command is None:
        raise KnowledgeProcessingError(KnowledgeFailure.OCR_UNAVAILABLE)
    environment = {
        "PATH": str(Path(command).parent),
        "OMP_THREAD_LIMIT": "1",
    }
    try:
        available = subprocess.run(
            [command, "--list-langs"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        raise KnowledgeProcessingError(KnowledgeFailure.TIMEOUT) from None
    except OSError:
        raise KnowledgeProcessingError(KnowledgeFailure.OCR_UNAVAILABLE) from None
    # Tesseract 部分语言包缺失时仍可能返回成功，必须明确确认全部配置语言可用。
    if (
        available.returncode
        or len(available.stdout) > 65536
        or not set(runtime["languages"].split("+")).issubset(available.stdout.decode(errors="replace").splitlines())
    ):
        raise KnowledgeProcessingError(KnowledgeFailure.OCR_UNAVAILABLE)
    with tempfile.TemporaryDirectory(prefix="y-point-pdf-") as directory:
        image = Path(directory) / "page.png"
        image.write_bytes(_render_page(page, runtime["dpi"]))
        target = Path(directory) / "recognized"
        try:
            result = subprocess.run(
                [command, str(image), str(target), "-l", runtime["languages"], "--psm", "3", "tsv"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=35,
                check=False,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            raise KnowledgeProcessingError(KnowledgeFailure.TIMEOUT) from None
        except OSError:
            raise KnowledgeProcessingError(KnowledgeFailure.OCR_UNAVAILABLE) from None
        output = target.with_suffix(".tsv")
        if result.returncode or not output.is_file():
            raise KnowledgeProcessingError(KnowledgeFailure.OCR_UNAVAILABLE)
        if output.stat().st_size > MAX_OCR_BYTES:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        data = output.read_text(encoding="utf-8")
    lines, weighted, total, low = {}, 0.0, 0, 0
    scale = 72 / runtime["dpi"]
    try:
        for row in csv.DictReader(io.StringIO(data), delimiter="\t"):
            text = row["text"].strip()
            if not text or row["level"] != "5":
                continue
            confidence = float(row["conf"])
            if not math.isfinite(confidence) or not 0 <= confidence <= 100:
                raise ValueError
            weight = len(text)
            weighted += confidence * weight
            total += weight
            low += weight if confidence < runtime["min_confidence"] else 0
            key = (row["block_num"], row["par_num"], row["line_num"])
            x, y, w, h = [float(row[field]) * scale for field in ("left", "top", "width", "height")]
            if not all(math.isfinite(value) and value >= 0 for value in (x, y, w, h)) or x + w > page.width + 2 or y + h > page.height + 2:
                raise ValueError
            lines.setdefault(key, []).append(
                {
                    "text": text,
                    "x0": x,
                    "top": y,
                    "x1": x + w,
                    "bottom": y + h,
                },
            )
    except (ValueError, KeyError, TypeError):
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED) from None
    confidence = weighted / total if total else 0
    if not total or confidence < runtime["min_confidence"] or low / total > 0.25:
        raise KnowledgeProcessingError(KnowledgeFailure.OCR_LOW_CONFIDENCE)
    return [(" ".join(word["text"] for word in words), _bbox(words)) for words in lines.values()], round(confidence, 2)


def _native_evidence(page, words: list[dict]) -> list[tuple[str, list[float], KnowledgePdfLayout, str | None]]:
    """提取有线框表格及版面正文，表格整行保留且不重复索引同一区域。"""
    tables = page.find_tables()
    if len(tables) > 100:
        raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
    result = []
    for number, table in enumerate(tables, 1):
        rows = table.extract()
        if not rows:
            continue
        if len(rows) > 2000 or max(len(row) for row in rows) > 128:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        for row_number, (values, row) in enumerate(zip(rows, table.rows, strict=True), 1):
            text = "；".join(f"列{column}={_normalize(str(value)) if value is not None else '[空单元格]'}" for column, value in enumerate(values, 1))
            if any(value is not None and str(value).strip() for value in values):
                result.append((f"表{number}；行{row_number}；{text}", list(row.bbox), KnowledgePdfLayout.TABLE, f"表{number}"))
    remaining = [
        word
        for word in words
        if not any(
            table.bbox[0] <= (word["x0"] + word["x1"]) / 2 <= table.bbox[2] and table.bbox[1] <= (word["top"] + word["bottom"]) / 2 <= table.bbox[3]
            for table in tables
        )
    ]
    result.extend((text, bbox, KnowledgePdfLayout.TEXT, None) for text, bbox in _native_lines(remaining, (page.bbox[0] + page.bbox[2]) / 2))
    ordered = sorted(result, key=lambda item: (round(item[1][1] / 3), item[1][0])) if tables else result
    origin_x, origin_y = page.bbox[:2]
    return [(text, [box[0] - origin_x, box[1] - origin_y, box[2] - origin_x, box[3] - origin_y], layout, table) for text, box, layout, table in ordered]


def parse_pdf_knowledge(content: bytes, options: dict | None = None) -> ParsedKnowledge:
    """按页判断文字、扫描、混合与空白，OCR 不可靠时整份文档拒绝发布。"""
    import pdfplumber
    from pypdf import PdfReader

    options = dict(options or {})
    runtime = options.pop("_pdf_runtime", None) or pdf_runtime_configuration()
    mode = KnowledgePdfMode(options.get("pdf_mode", KnowledgePdfMode.AUTO))
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
    parts, blocks, pages, offset, ocr_count = [], [], [], 0, 0
    with pdfplumber.open(io.BytesIO(content)) as document:
        for number, page in enumerate(document.pages, 1):
            if len(page.chars) > MAX_TEXT_CHARACTERS or len(page.images) > 1000:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            words = page.extract_words(x_tolerance=2, y_tolerance=3)
            image_area = sum(
                max(0, min(page.bbox[2], image["x1"]) - max(page.bbox[0], image["x0"]))
                * max(0, min(page.bbox[3], image["bottom"]) - max(page.bbox[1], image["top"]))
                for image in page.images
            )
            has_image = image_area >= page.width * page.height * 0.12
            visual = bool(page.images) or bool(page.curves or page.lines or page.rects)
            kind = (
                KnowledgePdfPageKind.MIXED
                if words and has_image
                else KnowledgePdfPageKind.TEXT
                if words
                else KnowledgePdfPageKind.SCAN
                if visual
                else KnowledgePdfPageKind.BLANK
            )
            use_ocr = kind != KnowledgePdfPageKind.BLANK and (
                mode == KnowledgePdfMode.OCR or mode == KnowledgePdfMode.AUTO and kind in (KnowledgePdfPageKind.SCAN, KnowledgePdfPageKind.MIXED)
            )
            if kind == KnowledgePdfPageKind.BLANK:
                pages.append(
                    {
                        "page": number,
                        "kind": kind,
                        "method": None,
                        "confidence": None,
                        "blocks": 0,
                    },
                )
                continue
            if kind == KnowledgePdfPageKind.SCAN and not use_ocr:
                raise KnowledgeProcessingError(KnowledgeFailure.OCR_REQUIRED)
            confidence = None
            if use_ocr:
                ocr_count += 1
                if ocr_count > runtime["max_ocr_pages"]:
                    raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
                lines, confidence = _ocr_page(page, runtime)
                evidence = [(text, bbox, KnowledgePdfLayout.UNVERIFIED, None) for text, bbox in lines]
                method = KnowledgePdfExtraction.OCR
            else:
                evidence = _native_evidence(page, words)
                method = KnowledgePdfExtraction.NATIVE
            beginning = len(blocks)
            for text, bbox, layout, table in evidence:
                text = _normalize_pdf_line(text)
                if not text:
                    continue
                if len(text.encode()) > 8192:
                    raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
                if parts:
                    parts.append("\n")
                    offset += 1
                start = offset
                parts.append(text)
                offset += len(text)
                if offset > MAX_TEXT_CHARACTERS or len(blocks) >= 10000:
                    raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
                blocks.append(
                    {
                        "start": start,
                        "end": offset,
                        "page": number,
                        "heading": f"第 {number} 页" + (f" · {table}" if table else ""),
                        "source_locator": {
                            "kind": KnowledgeSourceKind.PDF,
                            "format": "pdf",
                            "page": number,
                            "page_kind": kind,
                            "extraction_method": method,
                            "bbox": [round(value, 2) for value in bbox],
                            "page_width": float(page.width),
                            "page_height": float(page.height),
                            "confidence": confidence,
                            "layout": layout,
                            "table": table,
                            "review_required": use_ocr,
                        },
                    },
                )
            if len(blocks) == beginning:
                raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
            pages.append(
                {
                    "page": number,
                    "kind": kind,
                    "method": method,
                    "confidence": confidence,
                    "blocks": len(blocks) - beginning,
                },
            )
            page.close()
    if not blocks:
        raise KnowledgeProcessingError(KnowledgeFailure.OCR_REQUIRED)
    return ParsedKnowledge(
        "".join(parts),
        blocks,
        {
            "parser": PDF_PARSER_VERSION,
            "pages": pages,
            "ocr_pages": ocr_count,
            "blank_pages": sum(page["kind"] == KnowledgePdfPageKind.BLANK for page in pages),
            "languages": runtime["languages"] if ocr_count else None,
        },
    )


def render_pdf_page(content: bytes, number: int) -> bytes:
    """只渲染已鉴权引用对应的原始 PDF 页，不读取外部文件或链接。"""
    import pdfplumber
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or not 1 <= number <= len(reader.pages) <= MAX_PDF_PAGES:
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
    with pdfplumber.open(io.BytesIO(content)) as document:
        return _render_page(document.pages[number - 1], 120)
