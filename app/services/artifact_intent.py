# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 08:49:25
# @Author  : yangyuexiong
# @File    : artifact_intent.py

"""识别用户明确要求交付文件的文本指令。"""

import re

_ACTION = re.compile(
    "生成|创建|制作|导出|保存为|输出(?:为)?|写成|做成|做一份|整理成|给我一份|提供一份|转(?:换)?(?:成|为)|\\b(?:generate|create|export)\\b",
    re.I,
)
_NOT_REQUEST = re.compile(r"(?:不要|不用|不必|无需|别|请勿|如何|怎么|怎样|为什么|是否|能否)(?:帮我|为我|给我)?\s*$", re.I)
_FORMAT = re.compile("(?<![a-z])\\.?((?:docx|xlsx|pdf|txt|md|html|json|csv))(?![a-z])|(?<![a-z])(?:Word|Excel|Markdown)(?![a-z])", re.I)
_FILE = re.compile(r"文件|文档|表格|报告|下载|附件|file|document|spreadsheet|report", re.I)
_CLAUSE_END = re.compile(r"[，。！？；\n!?;]")
_ALIASES = {"word": ".docx", "excel": ".xlsx", "markdown": ".md"}


def requested_artifact_suffixes(text: str) -> frozenset[str] | None:
    """返回明确要求的格式；空集合表示要求文件但未指定格式，None 表示不强制。"""
    suffixes: set[str] = set()
    generic_file = False
    for action in _ACTION.finditer(text):
        if _NOT_REQUEST.search(text[max(0, action.start() - 10) : action.start()]):
            continue
        tail = text[action.end() : action.end() + 60]
        if tail.lstrip().startswith(("的", "过的")):
            continue
        tail = _CLAUSE_END.split(tail, maxsplit=1)[0]
        formats = list(_FORMAT.finditer(tail))
        if formats:
            for found in formats:
                label = (found.group(1) or found.group(0)).lower()
                suffixes.add(_ALIASES.get(label, "." + label))
        elif _FILE.search(tail):
            generic_file = True
    if suffixes:
        return frozenset(suffixes)
    return frozenset() if generic_file else None
