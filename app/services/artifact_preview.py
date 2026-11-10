# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 10:38:44
# @Author  : yangyuexiong
# @File    : artifact_preview.py

"""将已授权文件转换为有界、无外部资源的只读内容预览。"""

import csv
import html
import io
import json
from zipfile import ZipFile

import bleach
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph
from openpyxl import load_workbook
from pypdf import PdfReader

from app.core.enums import ArtifactPreviewKind
from app.schemas.artifact import ArtifactPreviewOut, ArtifactPreviewSheet

MAX_PREVIEW_CHARS = 100_000
MAX_PREVIEW_ROWS = 200
MAX_PREVIEW_COLUMNS = 50
MAX_PREVIEW_CELLS = 5_000
MAX_PREVIEW_SHEETS = 5
MAX_PREVIEW_PAGES = 20
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PREVIEW_MEDIA_TYPES = frozenset(
    {"text/plain", "text/markdown", "text/html", "text/csv", "application/json", "application/pdf", DOCX_MEDIA_TYPE, XLSX_MEDIA_TYPE},
)
HTML_TAGS = frozenset(
    {
        "p",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "em",
        "blockquote",
        "ul",
        "ol",
        "li",
        "pre",
        "code",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "a",
    },
)


class _PreviewBudget:
    """所有页面、段落及单元格共用的文本预算。"""

    def __init__(self) -> None:
        """初始化字符预算、单元格计数和截断标识。"""
        self.remaining = MAX_PREVIEW_CHARS
        self.cells = 0
        self.truncated = False

    def take(self, value: str) -> str:
        """收集剩余预算内的字符，并显式记录截断。"""
        if len(value) > self.remaining:
            self.truncated = True
        result = value[: self.remaining]
        self.remaining -= len(result)
        return result

    def rows(self, source) -> list[list[str]]:
        """有界读取行列，仅返回文本，不运行公式或访问外部关系。"""
        result = []
        for index, row in enumerate(source):
            if index >= MAX_PREVIEW_ROWS or self.cells >= MAX_PREVIEW_CELLS or self.remaining <= 0:
                self.truncated = True
                break
            if len(row) > MAX_PREVIEW_COLUMNS:
                self.truncated = True
            values = []
            for value in row[:MAX_PREVIEW_COLUMNS]:
                if self.cells >= MAX_PREVIEW_CELLS or self.remaining <= 0:
                    self.truncated = True
                    break
                values.append(self.take("" if value is None else str(value)))
                self.cells += 1
            result.append(values)
        return result


def _check_office(content: bytes) -> None:
    """在解析 Office XML 前检查解压体积、条目数和加密标志。"""
    with ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if len(entries) > 1000 or sum(item.file_size for item in entries) > 50 * 1024 * 1024:
            raise ValueError("Office 解压内容超过预览限制")
        if any(item.flag_bits & 1 for item in entries):
            raise ValueError("无法预览加密 Office 文件")


def _html_table(rows: list[list[str]]) -> str:
    """把表格中的纯文本转换为无属性的静态 HTML。"""
    return "<table><tbody>" + "".join("<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>" for row in rows) + "</tbody></table>"


def _document_html(content: bytes, budget: _PreviewBudget) -> str:
    """按原顺序保留 DOCX 正文标题、段落和表格，不加载图片或外部关系。"""
    _check_office(content)
    document = Document(io.BytesIO(content))
    parts = []
    for index, block in enumerate(document.iter_inner_content()):
        if index >= 5000 or budget.remaining <= 0:
            budget.truncated = True
            break
        if isinstance(block, Paragraph):
            style = block.style.name if block.style else ""
            tag = "p"
            if style == "Title":
                tag = "h1"
            elif style.startswith("Heading ") and style[-1:] in "123456":
                tag = f"h{style[-1]}"
            text = html.escape(budget.take(block.text)).replace("\n", "<br>")
            if style.startswith("List"):
                text = "• " + text
            parts.append(f"<{tag}>{text}</{tag}>")
        elif isinstance(block, Table):
            if len(block.columns) > MAX_PREVIEW_COLUMNS:
                budget.truncated = True
            rows = ([cell.text for cell in row.cells[:MAX_PREVIEW_COLUMNS]] for row in block.rows)
            parts.append(_html_table(budget.rows(rows)))
    return "".join(parts)


def _pdf_html(content: bytes, budget: _PreviewBudget) -> str:
    """按页提取 PDF 文本，扫描图片和原始版式通过原件查看。"""
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted:
        raise ValueError("无法预览加密 PDF")
    if len(reader.pages) > MAX_PREVIEW_PAGES:
        budget.truncated = True
    parts = []
    for index, page in enumerate(reader.pages[:MAX_PREVIEW_PAGES]):
        if budget.remaining <= 0:
            budget.truncated = True
            break
        value = budget.take(page.extract_text() or "本页没有可提取的文字，请下载查看图片内容。")
        parts.append(f"<h2>第 {index + 1} 页</h2><pre>{html.escape(value)}</pre>")
    return "".join(parts)


def build_artifact_preview(artifact_id: str, filename: str, media_type: str, content: bytes) -> ArtifactPreviewOut:
    """根据已核验媒体类型生成预览，调用方先完成文件授权和有界读取。"""
    if media_type not in PREVIEW_MEDIA_TYPES:
        raise ValueError("不支持的预览类型")
    budget = _PreviewBudget()
    result = ArtifactPreviewOut(artifact_id=artifact_id, filename=filename, media_type=media_type, kind=ArtifactPreviewKind.TEXT)
    if media_type == DOCX_MEDIA_TYPE:
        result.kind = ArtifactPreviewKind.HTML
        result.content = _document_html(content, budget)
        result.notice = "DOCX 内容预览保留正文与表格；图片、页眉页脚及精细版式请下载原件查看。"
    elif media_type == "application/pdf":
        result.kind = ArtifactPreviewKind.HTML
        result.content = _pdf_html(content, budget)
        result.notice = "PDF 文字内容预览；扫描页、图片和原始版式请下载原件查看。"
    elif media_type == XLSX_MEDIA_TYPE:
        _check_office(content)
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            result.kind = ArtifactPreviewKind.SHEETS
            if len(workbook.worksheets) > MAX_PREVIEW_SHEETS:
                budget.truncated = True
            for sheet in workbook.worksheets[:MAX_PREVIEW_SHEETS]:
                if sheet.max_row and sheet.max_row > MAX_PREVIEW_ROWS or sheet.max_column and sheet.max_column > MAX_PREVIEW_COLUMNS:
                    budget.truncated = True
                source = sheet.iter_rows(
                    max_row=min(sheet.max_row or MAX_PREVIEW_ROWS + 1, MAX_PREVIEW_ROWS + 1),
                    max_col=min(sheet.max_column or MAX_PREVIEW_COLUMNS, MAX_PREVIEW_COLUMNS),
                    values_only=True,
                )
                result.sheets.append(ArtifactPreviewSheet(name=sheet.title, rows=budget.rows(source)))
            result.notice = "表格仅展示单元格内容，不计算公式或加载外部链接；样式与图表请下载原件查看。"
        finally:
            workbook.close()
    else:
        text = content.decode("utf-8-sig")
        if media_type == "text/csv":
            result.kind = ArtifactPreviewKind.SHEETS
            result.sheets = [ArtifactPreviewSheet(name="CSV", rows=budget.rows(csv.reader(io.StringIO(text), strict=True)))]
        elif media_type == "application/json":
            if len(text) <= MAX_PREVIEW_CHARS:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            result.content = budget.take(text)
        elif media_type == "text/html":
            result.kind = ArtifactPreviewKind.HTML
            result.content = bleach.clean(budget.take(text), tags=HTML_TAGS, attributes={}, strip=True)
            result.notice = "HTML 使用只读隔离预览，不执行脚本或加载外部资源。"
        else:
            result.kind = ArtifactPreviewKind.MARKDOWN if media_type == "text/markdown" else ArtifactPreviewKind.TEXT
            result.content = budget.take(text)
    result.truncated = budget.truncated
    return result
