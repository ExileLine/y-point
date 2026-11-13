# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:08:18
# @Author  : yangyuexiong
# @File    : knowledge_formats.py

"""集中定义知识库专用扩展格式，不扩大普通对话附件的上传范围。"""

ROUTED_PARSER_VERSION = "structured-formats-v1"
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
KNOWLEDGE_EXTRA_MEDIA_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".pptx": PPTX_MEDIA_TYPE,
}
ROUTED_MEDIA_TYPES = frozenset(KNOWLEDGE_EXTRA_MEDIA_TYPES.values())
