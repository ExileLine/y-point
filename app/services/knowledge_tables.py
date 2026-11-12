# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 14:52:16
# @Author  : yangyuexiong
# @File    : knowledge_tables.py

"""把表格转换为带字段名和精确来源的完整记录，不计算公式或猜测统计口径。"""

import csv
import io
import json
import unicodedata
import zipfile
from datetime import date, datetime, time

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries

from app.core.enums import KnowledgeFailure, KnowledgeSourceKind
from app.schemas.knowledge import KnowledgeDocumentOptions
from app.services.knowledge_parser import KnowledgeProcessingError, ParsedKnowledge, MAX_TEXT_CHARACTERS

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TABLE_MEDIA_TYPES = frozenset((XLSX_MEDIA_TYPE, "text/csv"))
TABLE_PARSER_VERSION = "tables-v1"
MAX_TABLE_ROWS = 20000
MAX_TABLE_COLUMNS = 200
MAX_TABLE_CELLS = 100000


def _value(value: object) -> str:
    """稳定序列化单元格，保留换行和原始数值而不执行内容。"""
    if value is None:
        return ""
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    return unicodedata.normalize("NFC", str(value)).replace("\x00", "")


def _build_parsed(tables: list[dict]) -> ParsedKnowledge:
    """保留完整表头和每条记录，片段偏移以规范证据文本为准。"""
    lines, blocks, offset = [], [], 0
    for table in tables:
        rows = table["rows"]
        header_count = table["header_rows"]
        if len(rows) < header_count:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        width = max(len(row[1]) for row in rows)
        headers = []
        for column in range(width):
            parts = []
            for _, cells, _ in rows[:header_count]:
                part = _value(cells[column]) if column < len(cells) else ""
                if part and part not in parts:
                    parts.append(part)
            # 列坐标始终参与名称，重复表头也能准确区分。
            headers.append(f"{' / '.join(parts) or '未命名列'}[{get_column_letter(table['columns'][column])}]")
        for row_number, values, physical_end in rows[header_count:]:
            if not any(_value(value) for value in values):
                continue
            fields = [f"{headers[column]}={json.dumps(_value(value), ensure_ascii=False)}" for column, value in enumerate(values)]
            prefix = f"工作表：{table['sheet']}；区域：{table['region']}；第 {row_number} 行；"
            line = prefix + "；".join(fields)
            # 不把长行截断为没有表头的任意字符串，也不静默丢弃字段。
            if len(line) > 2000:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            if lines:
                lines.append("\n")
                offset += 1
            start = offset
            lines.append(line)
            offset += len(line)
            if offset > MAX_TEXT_CHARACTERS or len(blocks) >= 10000:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            locator = {
                "kind": KnowledgeSourceKind.TABLE,
                "sheet": table["sheet"],
                "table_range": table["region"],
                "header_range": table["header_range"],
                "range": f"{get_column_letter(table['first_column'])}{row_number}:{get_column_letter(table['columns'][-1])}{physical_end}",
                "columns": [get_column_letter(column) for column in table["columns"]],
                "row_start": row_number,
                "row_end": physical_end,
                "format": table["format"],
                "formula_policy": "cached_values_and_formula_text",
                "hidden_excluded": table.get("hidden_excluded", False),
            }
            blocks.append(
                {
                    "start": start,
                    "end": offset,
                    "page": None,
                    "heading": f"{table['sheet']} · {table['region']}",
                    "source_locator": locator,
                },
            )
    if not blocks:
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
    return ParsedKnowledge("".join(lines), blocks)


def _csv_tables(content: bytes, options: KnowledgeDocumentOptions) -> list[dict]:
    """读取 UTF-8 CSV 的逻辑记录，引用保留含多行字段的物理行范围。"""
    if options.sheet_names:
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
    text = content.decode("utf-8-sig")
    csv.field_size_limit(500000)
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text, newline=""), dialect=dialect, strict=True)
    rows, previous, width, cells = [], 0, None, 0
    for values in reader:
        start, previous = previous + 1, reader.line_num
        if not values or not any(value.strip() for value in values):
            continue
        width = len(values) if width is None else width
        if len(values) != width:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        cells += len(values)
        if len(rows) >= MAX_TABLE_ROWS or width > MAX_TABLE_COLUMNS or cells > MAX_TABLE_CELLS:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        rows.append((start, values, previous))
    if not rows:
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
    return [
        {
            "sheet": "CSV",
            "region": f"A1:{get_column_letter(width)}{reader.line_num}",
            "first_column": 1,
            "columns": list(range(1, width + 1)),
            "header_range": f"A{rows[0][0]}:{get_column_letter(width)}{rows[min(options.header_rows, len(rows)) - 1][2]}",
            "header_rows": options.header_rows,
            "rows": rows,
            "format": "csv",
        },
    ]


def _xlsx_tables(content: bytes, options: KnowledgeDocumentOptions) -> list[dict]:
    """优先读取正式 Excel Table，否则按空白行识别区域；隐藏内容排除并禁止公式执行。"""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if len(infos) > 4096 or sum(info.file_size for info in infos) > 32 * 1024 * 1024:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        if any("vbaproject.bin" in item.filename.lower() or item.flag_bits & 1 for item in infos):
            raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)
    book = load_workbook(io.BytesIO(content), data_only=False, keep_links=False)
    cached = load_workbook(io.BytesIO(content), data_only=True, keep_links=False)
    tables, total_cells = [], 0
    try:
        available = {sheet.title for sheet in book if sheet.sheet_state == "visible"}
        if not set(options.sheet_names) <= available:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        for sheet in book:
            if sheet.title not in available or options.sheet_names and sheet.title not in options.sheet_names:
                continue
            if sheet.max_row > MAX_TABLE_ROWS or sheet.max_column > MAX_TABLE_COLUMNS:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            total_cells += sheet.max_row * sheet.max_column
            if total_cells > MAX_TABLE_CELLS:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            hidden_columns = set()
            for dimension in sheet.column_dimensions.values():
                if dimension.hidden:
                    beginning = dimension.min or column_index_from_string(dimension.index)
                    ending = min(dimension.max or beginning, sheet.max_column)
                    hidden_columns.update(range(beginning, ending + 1))
            merged = {}
            for area in sheet.merged_cells.ranges:
                for row in range(area.min_row, area.max_row + 1):
                    for column in range(area.min_col, area.max_col + 1):
                        merged[row, column] = sheet.cell(area.min_row, area.min_col).value
                        if len(merged) > MAX_TABLE_CELLS:
                            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            regions = [range_boundaries(table.ref) for table in sheet.tables.values()]
            if not regions:
                beginning = None
                for row in range(1, sheet.max_row + 2):
                    populated = row <= sheet.max_row and any(cell.value is not None for cell in sheet[row])
                    if populated and beginning is None:
                        beginning = row
                    if not populated and beginning is not None:
                        regions.append((1, beginning, sheet.max_column, row - 1))
                        beginning = None
            for left, top, right, bottom in regions:
                rows = []
                # 表头禁止隐藏；否则不能证明每个值所属字段。
                if any(sheet.row_dimensions[row].hidden for row in range(top, min(bottom + 1, top + options.header_rows))):
                    raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
                visible_columns = [column for column in range(left, right + 1) if column not in hidden_columns]
                if not visible_columns:
                    continue
                for row in range(top, bottom + 1):
                    if sheet.row_dimensions[row].hidden:
                        continue
                    values = []
                    for column in visible_columns:
                        cell = sheet.cell(row, column)
                        value = merged.get((row, column), cell.value)
                        if cell.data_type == "f":
                            result = cached[sheet.title].cell(row, column).value
                            value = f"公式：{cell.value}；缓存结果：{_value(result) if result is not None else '未提供，未计算'}"
                        elif isinstance(value, (int, float)) and not isinstance(value, bool) and cell.number_format != "General":
                            value = f"{value}（格式：{cell.number_format}）"
                        values.append(value)
                    rows.append((row, values, row))
                tables.append(
                    {
                        "sheet": sheet.title,
                        "region": f"{get_column_letter(left)}{top}:{get_column_letter(right)}{bottom}",
                        "first_column": visible_columns[0],
                        "columns": visible_columns,
                        "header_range": f"{get_column_letter(left)}{top}:{get_column_letter(right)}{top + options.header_rows - 1}",
                        "header_rows": options.header_rows,
                        "rows": rows,
                        "format": "xlsx",
                        "hidden_excluded": len(visible_columns) != right - left + 1 or any(sheet.row_dimensions[row].hidden for row in range(top, bottom + 1)),
                    },
                )
    finally:
        book.close()
        cached.close()
    return tables


def parse_table_knowledge(content: bytes, media_type: str, options: dict | None = None) -> ParsedKnowledge:
    """解析结构表格，所有返回内容均能定位到原始记录，不使用模型改写数值。"""
    selected = KnowledgeDocumentOptions.model_validate(options or {})
    tables = _xlsx_tables(content, selected) if media_type == XLSX_MEDIA_TYPE else _csv_tables(content, selected)
    return _build_parsed(tables)
