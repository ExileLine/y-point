# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:09:25
# @Author  : yangyuexiong
# @File    : knowledge_config.py

"""按配置对象及数组记录提取 JSON/YAML，固定逻辑路径而不执行标签或模板。"""

import json
import math
import re

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from app.core.enums import KnowledgeFailure, KnowledgeSourceKind
from app.services.knowledge_parser import KnowledgeProcessingError, ParsedKnowledge
from app.services.knowledge_sources import MAX_SOURCE_DEPTH, MAX_SOURCE_NODES, SourceKnowledgeBuilder


class ConfigurationLoader(yaml.SafeLoader):
    """限制 YAML 组合深度，保留标记位置及有界别名，拒绝任意对象构造。"""

    def __init__(self, stream):
        """复制安全解析器的词法规则，布尔仅接受 true/false，日期保留文本。"""
        super().__init__(stream)
        self.depth = 0
        self.nodes = 0
        self.yaml_implicit_resolvers = {
            key: [(tag, pattern) for tag, pattern in resolvers if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}]
            for key, resolvers in self.yaml_implicit_resolvers.items()
        }
        self.add_boolean_resolver()

    def add_boolean_resolver(self) -> None:
        """仅修改当前实例，防止改写其他业务的 YAML 隐式解析规则。"""
        for initial in "tTfF":
            self.yaml_implicit_resolvers.setdefault(initial, []).append(("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I)))

    def compose_node(self, parent, index):
        """在递归组合前检查深度和节点数，别名实际展开另有计量。"""
        self.depth += 1
        self.nodes += 1
        try:
            if self.depth > MAX_SOURCE_DEPTH or self.nodes > MAX_SOURCE_NODES:
                raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1


def _unique_json_pairs(pairs: list[tuple[str, object]]) -> dict:
    """拒绝 JSON 重复键，避免原始值被覆盖后仍发布看似完整的配置。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        result[key] = value
    return result


def _json_node(value, depth: int = 0, budget: list[int] | None = None):
    """将已校验的 JSON 转为统一节点，后续路径与分组逻辑不依赖格式。"""
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if depth > MAX_SOURCE_DEPTH or budget[0] > MAX_SOURCE_NODES:
        raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
    if isinstance(value, dict):
        return MappingNode(
            "tag:yaml.org,2002:map",
            [(ScalarNode("tag:yaml.org,2002:str", key), _json_node(item, depth + 1, budget)) for key, item in value.items()],
        )
    if isinstance(value, list):
        return SequenceNode("tag:yaml.org,2002:seq", [_json_node(item, depth + 1, budget) for item in value])
    if isinstance(value, float) and not math.isfinite(value):
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
    tag = (
        "null" if value is None else "bool" if isinstance(value, bool) else "int" if isinstance(value, int) else "float" if isinstance(value, float) else "str"
    )
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
    return ScalarNode(f"tag:yaml.org,2002:{tag}", text)


def _pointer(parent: str, key: str) -> str:
    """使用 JSON Pointer 转义路径段，区分斜杠、波浪号和数组下标。"""
    return f"{parent}/{key.replace('~', '~0').replace('/', '~1')}"


def parse_configuration(content: bytes, media_type: str) -> ParsedKnowledge:
    """保留父对象内的字段关系，数组对象独立成记录，不跨配置路径混合。"""
    format_name = "json" if media_type == "application/json" else "yaml"
    builder = SourceKnowledgeBuilder(format_name)
    try:
        text = content.decode("utf-8-sig")
        if format_name == "json":
            root = _json_node(json.loads(text, object_pairs_hook=_unique_json_pairs))
        else:
            loader = ConfigurationLoader(text)
            try:
                root = loader.get_single_node()
            finally:
                loader.dispose()
        if root is None:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        budget = [0]
        _walk_configuration(root, "", builder, set(), budget)
    except (yaml.YAMLError, ValueError, TypeError, UnicodeError, RecursionError) as failure:
        if isinstance(failure, KnowledgeProcessingError):
            raise
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE) from None
    builder.metadata["visited_nodes"] = budget[0]
    builder.metadata["path_format"] = "json-pointer"
    return builder.finish()


def _walk_configuration(node, pointer: str, builder: SourceKnowledgeBuilder, active: set, budget: list[int], depth: int = 0) -> None:
    """展开有界非循环别名，拒绝合并键、重复键、自定义标签和非字符串对象键。"""
    budget[0] += 1
    if budget[0] > MAX_SOURCE_NODES or depth > MAX_SOURCE_DEPTH:
        raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
    if id(node) in active:
        raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
    active.add(id(node))
    try:
        if isinstance(node, ScalarNode):
            _append_configuration([(pointer, node)], pointer.rsplit("/", 1)[0], builder)
            return
        tag = "map" if isinstance(node, MappingNode) else "seq" if isinstance(node, SequenceNode) else None
        if tag is None or node.tag != f"tag:yaml.org,2002:{tag}":
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        children = []
        if isinstance(node, MappingNode):
            keys = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.tag != "tag:yaml.org,2002:str" or key.value in keys:
                    raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
                keys.add(key.value)
                children.append((_pointer(pointer, key.value), value))
        else:
            children = [(_pointer(pointer, str(index)), item) for index, item in enumerate(node.value)]
        if not children:
            empty = ScalarNode(f"tag:yaml.org,2002:{tag}", "{}" if tag == "map" else "[]")
            _append_configuration([(pointer, empty)], pointer, builder)
            return
        if isinstance(node, SequenceNode):
            for path, child in children:
                _walk_configuration(child, path, builder, active, budget, depth + 1)
            return
        scalar_fields = [(path, child) for path, child in children if isinstance(child, ScalarNode)]
        budget[0] += len(scalar_fields)
        if budget[0] > MAX_SOURCE_NODES:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        if scalar_fields:
            _append_configuration(scalar_fields, pointer, builder)
        for path, child in children:
            if not isinstance(child, ScalarNode):
                _walk_configuration(child, path, builder, active, budget, depth + 1)
    finally:
        active.remove(id(node))


def _append_configuration(fields: list[tuple[str, ScalarNode]], parent: str, builder: SourceKnowledgeBuilder) -> None:
    """父对象内按有界字段批次保存路径、类型和值，超长单值明确失败。"""
    lines, paths, marks = [], [], []
    for pointer, node in fields:
        kind = node.tag.removeprefix("tag:yaml.org,2002:")
        if kind not in {"str", "int", "float", "bool", "null", "map", "seq"}:
            raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        if kind not in {"map", "seq"}:
            try:
                value = yaml.constructor.SafeConstructor().construct_object(node)
            except (ValueError, TypeError, KeyError, OverflowError):
                raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE) from None
            if isinstance(value, float) and not math.isfinite(value):
                raise KnowledgeProcessingError(KnowledgeFailure.INVALID_STRUCTURE)
        value = json.dumps(node.value, ensure_ascii=False) if kind == "str" else node.value
        line = f"{pointer or '[根值]'}（{kind}）= {value}"
        if len(line.encode()) > 7600:
            raise KnowledgeProcessingError(KnowledgeFailure.SIZE_LIMIT)
        if lines and len(("\n".join([*lines, line])).encode()) > 7600:
            _flush_configuration(lines, paths, marks, parent, builder)
            lines, paths, marks = [], [], []
        lines.append(line)
        paths.append(pointer)
        if node.start_mark is not None:
            marks.extend([node.start_mark.line + 1, max(node.start_mark.line + 1, node.end_mark.line + bool(node.end_mark.column))])
    _flush_configuration(lines, paths, marks, parent, builder)


def _flush_configuration(lines: list[str], paths: list[str], marks: list[int], parent: str, builder: SourceKnowledgeBuilder) -> None:
    """用逻辑路径而非执行结果描述配置，行号只在 YAML 实际标记可用时提供。"""
    source = {
        "kind": KnowledgeSourceKind.CONFIG,
        "format": builder.metadata["format"],
        "pointer": parent,
        "paths": list(paths),
        "line_start": min(marks) if marks else None,
        "line_end": max(marks) if marks else None,
    }
    builder.append("\n".join(lines), source, heading=f"配置路径 {parent or '[根对象]'}")
