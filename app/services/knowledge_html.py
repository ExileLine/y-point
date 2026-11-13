# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:10:42
# @Author  : yangyuexiong
# @File    : knowledge_html.py

"""提取上传 HTML 的主文、标题及表格，不加载网页资源或执行脚本。"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from app.core.enums import KnowledgeContentRole, KnowledgeFailure, KnowledgeSourceKind
from app.services.knowledge_parser import KnowledgeProcessingError, ParsedKnowledge, _normalize
from app.services.knowledge_sources import MAX_SOURCE_DEPTH, MAX_SOURCE_NODES, SourceKnowledgeBuilder, source_table_text

VOID_TAGS = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
NOISE_TAGS = frozenset(
    {"script", "style", "noscript", "template", "head", "nav", "header", "footer", "aside", "form", "button", "svg", "canvas", "iframe", "object"}
)
BLOCK_TAGS = frozenset(
    {"article", "main", "section", "div", "p", "li", "ul", "ol", "dl", "dt", "dd", "pre", "blockquote", "table", "h1", "h2", "h3", "h4", "h5", "h6"}
)


@dataclass
class HtmlNode:
    """保存轻量结构及源行号，所有内容仅作为文本数据处理。"""

    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    line: int = 1
    path: str = ""
    excluded: bool = False


class MainContentParser(HTMLParser):
    """构建有界文本树并提前标记噪声，避免在提取阶段回退到被排除内容。"""

    def __init__(self):
        """初始化非执行式 HTML 解析，保留可核对的元素路径与标题。"""
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("root")
        self.stack = [self.root]
        self.nodes = 0
        self.excluded = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """按元素、显式隐藏属性及导航角色排除噪声，处理常见省略结束标签。"""
        self.nodes += 1
        if self.nodes > MAX_SOURCE_NODES or len(self.stack) > MAX_SOURCE_DEPTH:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        if tag in {"p", "li", "td", "th", "tr"}:
            closable = {"td", "th"} if tag in {"td", "th"} else {tag}
            if self.stack[-1].tag in closable:
                self.stack.pop()
        attributes = dict(attrs)
        style = attributes.get("style") or ""
        parent = self.stack[-1]
        excluded = (
            parent.excluded
            or tag in NOISE_TAGS
            and not (tag == "header" and any(item.tag in {"article", "main"} or (item.attrs.get("role") or "").casefold() == "main" for item in self.stack))
            or "hidden" in attributes
            or (attributes.get("aria-hidden") or "").casefold() == "true"
            or (attributes.get("role") or "").casefold() in {"navigation", "banner", "contentinfo", "complementary"}
            or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", style, re.I))
        )
        node = HtmlNode(tag, attributes, line=self.getpos()[0], path=f"{parent.path}/{tag}[{self.nodes}]", excluded=excluded)
        parent.children.append(node)
        self.excluded += bool(excluded and not parent.excluded)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """保持自闭合标签的树深度，不让后续正文被错误吞入该节点。"""
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag and tag not in VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        """容忍常见不完整 HTML，并只关闭当前路径中实际存在的标签。"""
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        """保留文本节点，脚本及样式文本稍后随排除节点一起丢弃。"""
        self.nodes += 1
        if self.nodes > MAX_SOURCE_NODES:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        self.stack[-1].children.append(data)


def _raw_visible_text(node: HtmlNode) -> str:
    """只拼接可见文本节点，保留行内元素两侧空格供外层统一规范化。"""
    if node.excluded:
        return ""
    return "".join(child if isinstance(child, str) else "\n" if child.tag == "br" else _raw_visible_text(child) for child in node.children)


def _visible_text(node: HtmlNode, *, code: bool = False) -> str:
    """按原有行内顺序取文本，保留代码缩进并忽略隐藏或可执行区域。"""
    text = _raw_visible_text(node)
    return _normalize(text) if code else re.sub(r"\s+", " ", text).strip()


def _main_roots(node: HtmlNode, candidates: set[str]) -> list[HtmlNode]:
    """选取最外层主内容节点，避免嵌套 article 被重复提取。"""
    if node.excluded:
        return []
    if node.tag in candidates or "main" in candidates and node.attrs.get("role") == "main":
        return [node]
    return [item for child in node.children if isinstance(child, HtmlNode) for item in _main_roots(child, candidates)]


def _html_source(node: HtmlNode, heading: list[str], role: KnowledgeContentRole) -> dict:
    """返回静态元素定位信息，前端只显示文本，不构造可执行链接。"""
    return {
        "kind": KnowledgeSourceKind.HTML,
        "format": "html",
        "role": role,
        "element": node.tag,
        "element_id": (node.attrs.get("id") or "")[:512] or None,
        "dom_path": node.path,
        "line": node.line,
        "heading_path": list(heading),
    }


def _walk_html(node: HtmlNode, headings: list[str], builder: SourceKnowledgeBuilder, tables: list[int]) -> None:
    """按主文顺序生成标题、段落和完整表格行，段落间不重复祖先文本。"""
    if node.excluded:
        return
    if re.fullmatch(r"h[1-6]", node.tag):
        level = int(node.tag[1])
        title = _visible_text(node)[:512]
        if title:
            del headings[level - 1 :]
            headings.append(title)
            builder.append(title, _html_source(node, headings, KnowledgeContentRole.HEADING), heading=" / ".join(headings))
        return
    if node.tag == "table":
        tables[0] += 1
        rows = _table_rows(node)
        if len(rows) > 2000:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        headers = None
        for number, row in enumerate(rows, 1):
            cells = [child for child in row.children if isinstance(child, HtmlNode) and child.tag in {"td", "th"}]
            if len(cells) > 128:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            values = [_visible_text(cell) for cell in cells]
            if not any(values):
                continue
            source = _html_source(row, headings, KnowledgeContentRole.TABLE)
            source["table"] = tables[0]
            source["row"] = number
            source["merged_cells"] = any(cell.attrs.get("rowspan", "1") != "1" or cell.attrs.get("colspan", "1") != "1" for cell in cells)
            if number == 1 and cells and all(cell.tag == "th" and not cell.excluded for cell in cells) and not source["merged_cells"]:
                headers = values
            text = source_table_text(tables[0], number, values, headers if number > 1 and not source["merged_cells"] else None)
            builder.append(text, source, heading=" / ".join(headings))
        return
    if node.tag in {"p", "li", "dt", "dd", "blockquote", "pre"} and not any(
        isinstance(child, HtmlNode) and child.tag in BLOCK_TAGS - {"p"} for child in node.children
    ):
        role = KnowledgeContentRole.CODE if node.tag == "pre" else KnowledgeContentRole.TEXT
        builder.append_text(_visible_text(node, code=node.tag == "pre"), _html_source(node, headings, role), heading=" / ".join(headings))
        return
    buffer = []
    for child in node.children:
        if isinstance(child, str):
            buffer.append(child)
        else:
            if child.excluded:
                continue
            if child.tag not in BLOCK_TAGS and not _contains_blocks(child):
                buffer.append("\n" if child.tag == "br" else _raw_visible_text(child))
                continue
            if buffer:
                builder.append_text(re.sub(r"\s+", " ", "".join(buffer)), _html_source(node, headings, KnowledgeContentRole.TEXT), heading=" / ".join(headings))
                buffer = []
            _walk_html(child, headings, builder, tables)
    if buffer:
        builder.append_text(re.sub(r"\s+", " ", "".join(buffer)), _html_source(node, headings, KnowledgeContentRole.TEXT), heading=" / ".join(headings))


def _contains_blocks(node: HtmlNode) -> bool:
    """检测行内包装下的块元素，保持行内文字连贯且不吞掉嵌套正文。"""
    return any(isinstance(child, HtmlNode) and (child.tag in BLOCK_TAGS or _contains_blocks(child)) for child in node.children if not isinstance(child, str))


def _table_rows(node: HtmlNode) -> list[HtmlNode]:
    """只读取当前表格的行，嵌套表格拒绝混为同一记录。"""
    result = []
    for child in node.children:
        if not isinstance(child, HtmlNode) or child.excluded:
            continue
        if child.tag == "table":
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        if child.tag == "tr":
            _table_rows(child)
            result.append(child)
        else:
            result.extend(_table_rows(child))
    return result


def parse_html_knowledge(content: bytes) -> ParsedKnowledge:
    """优先 main，其次 article，否则读取去噪正文；不抓取 URL 或执行 JavaScript。"""
    parser = MainContentParser()
    parser.feed(content.decode("utf-8-sig"))
    parser.close()
    roots = _main_roots(parser.root, {"main"}) or _main_roots(parser.root, {"article"}) or _main_roots(parser.root, {"body"}) or [parser.root]
    builder = SourceKnowledgeBuilder("html")
    tables = [0]
    for node in roots:
        _walk_html(node, [], builder, tables)
    builder.metadata.update(
        {
            "selection": roots[0].tag,
            "excluded_regions": parser.excluded,
            "table_count": tables[0],
            "external_resources_loaded": False,
        },
    )
    return builder.finish()
