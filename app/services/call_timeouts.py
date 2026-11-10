# -*- coding: utf-8 -*-
# @Time    : 2026-11-09 21:56:41
# @Author  : yangyuexiong
# @File    : call_timeouts.py

"""统一 Agent 单次工具预算与部署分类时限的合成规则。"""

from app.core.config import get_config
from app.core.enums import ToolKind


def effective_tool_timeout(agent_timeout: int, *, kind: ToolKind | None = None, tool_name: str | None = None) -> int:
    """单次调用采用 Agent 已保存时限与对应业务分类时限的较小值。"""
    config = get_config()
    if kind == ToolKind.MCP:
        return min(agent_timeout, config.MCP_CALL_TIMEOUT_SECONDS)
    if kind == ToolKind.SCRIPT:
        return min(agent_timeout, config.SCRIPT_EXECUTION_TIMEOUT_SECONDS)
    if tool_name == "search_knowledge_base":
        return min(agent_timeout, config.KNOWLEDGE_SEARCH_TIMEOUT_SECONDS)
    return agent_timeout
