# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:11:53
# @Author  : yangyuexiong
# @File    : knowledge_slides.py

"""按演示文稿清单读取 PPTX 正文、表格与备注，仅访问包内已有 XML。"""

import io
import posixpath
import zipfile
from xml.etree import ElementTree as ET

from app.core.enums import KnowledgeContentRole, KnowledgeFailure, KnowledgeSourceKind
from app.services.knowledge_parser import KnowledgeProcessingError, ParsedKnowledge, _normalize
from app.services.knowledge_sources import MAX_SOURCE_NODES, SourceKnowledgeBuilder, source_table_text

PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
RELATIONSHIP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {
    "p": PRESENTATION_NS,
    "a": DRAWING_NS,
}
EXCLUDED_PLACEHOLDERS = frozenset({"dt", "ftr", "hdr", "sldNum"})


def _read_xml(archive: zipfile.ZipFile, name: str):
    """在解析前拒绝 DTD/实体声明和非 UTF-8 XML，禁止包外资源解析。"""
    try:
        text = archive.read(name).decode("utf-8-sig")
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        root = ET.fromstring(text)
        pending, count = [(root, 0)], 0
        while pending:
            node, depth = pending.pop()
            count += 1
            if count > MAX_SOURCE_NODES or depth > 48:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            pending.extend((child, depth + 1) for child in node)
        return root
    except (KeyError, UnicodeError, ET.ParseError):
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE) from None


def _relationships(archive: zipfile.ZipFile, part: str) -> dict:
    """解析内部部件关系，外部目标仅记录为不可读取，不发出网络请求。"""
    directory, filename = posixpath.split(part)
    relation_file = posixpath.join(directory, "_rels", filename + ".rels")
    if relation_file not in archive.namelist():
        return {}
    result, seen = {}, set()
    for row in _read_xml(archive, relation_file):
        identity = row.get("Id")
        if not identity or identity in seen:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        seen.add(identity)
        target = row.get("Target", "")
        if row.get("TargetMode") == "External":
            continue
        resolved = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join(directory, target))
        if not target or ":" in target or "\\" in target or resolved.startswith("../") or resolved not in archive.namelist():
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        result[identity] = (row.get("Type", "").rsplit("/", 1)[-1], resolved)
    return result


def _shape_text(shape) -> str:
    """按段落保留文本运行及显式换行，不读取图表缓存或替代文本当作正文。"""
    parts = []
    for paragraph in shape.findall("./p:txBody/a:p", NS):
        line = []
        for item in paragraph.iter():
            if item.tag == f"{{{DRAWING_NS}}}t":
                line.append(item.text or "")
            elif item.tag == f"{{{DRAWING_NS}}}br":
                line.append("\n")
        parts.append("".join(line))
    return _normalize("\n".join(parts))


def _shapes(tree):
    """展开分组的对象顺序，不将位置顺序宣称为复杂版面的阅读顺序。"""
    for shape in tree:
        if _shape_identity(shape)[2]:
            continue
        if shape.tag == f"{{{PRESENTATION_NS}}}grpSp":
            yield from _shapes(shape)
        elif shape.tag in {f"{{{PRESENTATION_NS}}}sp", f"{{{PRESENTATION_NS}}}graphicFrame"}:
            yield shape


def _shape_identity(shape) -> tuple[str, str | None, bool]:
    """读取形状标识与占位符类型，隐藏对象不会进入正文或备注。"""
    identity = shape.find(".//p:cNvPr", NS)
    placeholder = shape.find(".//p:ph", NS)
    return (
        identity.get("id", "") if identity is not None else "",
        placeholder.get("type", "body") if placeholder is not None else None,
        identity is not None and identity.get("hidden") in {"1", "true"},
    )


def _slide_units(root, builder: SourceKnowledgeBuilder, page: int, title: str, *, notes: bool = False) -> None:
    """正文与备注分别形成证据；表格按完整行保存，不猜测合并单元格含义。"""
    tree = root.find("./p:cSld/p:spTree", NS)
    if tree is None:
        return
    table_number = 0
    for shape in _shapes(tree):
        identity, placeholder, hidden = _shape_identity(shape)
        if hidden or placeholder in EXCLUDED_PLACEHOLDERS or notes and placeholder == "sldImg":
            continue
        role = KnowledgeContentRole.NOTES if notes else KnowledgeContentRole.HEADING if placeholder in {"title", "ctrTitle"} else KnowledgeContentRole.TEXT
        source = {
            "kind": KnowledgeSourceKind.SLIDE,
            "format": "pptx",
            "slide": page,
            "slide_title": title,
            "shape_id": identity,
            "role": role,
            "reading_order": "shape-tree",
        }
        text = _shape_text(shape)
        if text:
            builder.append_text(text, source, heading=f"第 {page} 页 · {title}" + (" · 演讲者备注" if notes else ""), page=page)
        for table in shape.findall(".//a:tbl", NS):
            table_number += 1
            rows = table.findall("a:tr", NS)
            if len(rows) > 2000:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            properties = table.find("a:tblPr", NS)
            has_header = properties is not None and properties.get("firstRow") in {"1", "true"}
            headers = None
            for number, row in enumerate(rows, 1):
                cells = row.findall("a:tc", NS)
                if len(cells) > 128:
                    raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
                values = [
                    "\n".join("".join(item.text or "" for item in paragraph.findall(".//a:t", NS)) for paragraph in cell.findall(".//a:p", NS))
                    for cell in cells
                ]
                if not any(values):
                    continue
                locator = {
                    **source,
                    "role": KnowledgeContentRole.NOTES if notes else KnowledgeContentRole.TABLE,
                    "table": table_number,
                    "row": number,
                    "merged_cells": any(
                        cell.get("gridSpan", "1") != "1"
                        or cell.get("rowSpan", "1") != "1"
                        or cell.get("hMerge") in {"1", "true"}
                        or cell.get("vMerge") in {"1", "true"}
                        for cell in cells
                    ),
                }
                if number == 1 and has_header and not locator["merged_cells"]:
                    headers = values
                text = source_table_text(table_number, number, values, headers if number > 1 and not locator["merged_cells"] else None)
                builder.append(text, locator, heading=f"第 {page} 页 · {title}" + (" · 演讲者备注" if notes else ""), page=page)


def parse_pptx_knowledge(content: bytes) -> ParsedKnowledge:
    """按 sldIdLst 实际顺序读取可见幻灯片及关联备注，不执行宏或嵌入对象。"""
    builder = SourceKnowledgeBuilder("pptx")
    hidden = notes_count = unsupported = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(infos) > 1000 or len(set(names)) != len(names) or sum(item.file_size for item in infos) > 32 * 1024 * 1024:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            if any("vbaproject.bin" in name.casefold() or ".." in name.split("/") or "\\" in name or name.startswith("/") for name in names):
                raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
            presentation = "ppt/presentation.xml"
            slides = _read_xml(archive, presentation).findall("./p:sldIdLst/p:sldId", NS)
            if not slides:
                raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
            if len(slides) > 200:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            relationships = _relationships(archive, presentation)
            for page, slide in enumerate(slides, 1):
                relation = relationships.get(slide.get(f"{{{RELATIONSHIP_NS}}}id"))
                if relation is None or relation[0] != "slide":
                    raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
                root = _read_xml(archive, relation[1])
                if root.tag != f"{{{PRESENTATION_NS}}}sld":
                    raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
                if root.get("show", "1") in {"0", "false"}:
                    hidden += 1
                    continue
                title = ""
                tree = root.find("./p:cSld/p:spTree", NS)
                if tree is not None:
                    for shape in _shapes(tree):
                        _, placeholder, excluded = _shape_identity(shape)
                        if not excluded and placeholder in {"title", "ctrTitle"}:
                            title = _shape_text(shape)[:512]
                            break
                _slide_units(root, builder, page, title or "未命名幻灯片")
                unsupported += len(root.findall(".//p:pic", NS)) + len(root.findall(".//a:graphicData", NS)) - len(root.findall(".//a:tbl", NS))
                for kind, target in _relationships(archive, relation[1]).values():
                    if kind == "notesSlide":
                        beginning = len(builder.blocks)
                        notes_root = _read_xml(archive, target)
                        if notes_root.tag != f"{{{PRESENTATION_NS}}}notes":
                            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
                        _slide_units(notes_root, builder, page, title or "未命名幻灯片", notes=True)
                        notes_count += len(builder.blocks) - beginning
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError):
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED) from None
    builder.metadata.update(
        {
            "slide_count": len(slides),
            "hidden_slides": hidden,
            "notes_blocks": notes_count,
            "unsupported_visual_objects": unsupported,
            "reading_order": "shape-tree",
            "warnings": ["图片、图表及嵌入对象不作语义识别；复杂版面按对象顺序提取，请核对原文件。"],
        },
    )
    return builder.finish()
