# -*- coding: utf-8 -*-
# @Time    : 2026-11-23
# @Author  : Codex
# @File    : mcp_tool_search.py

"""在 Run 固定工具集合内搜索，并持久化下一次模型请求的 MCP Schema 选择。"""

import json
import re
from typing import Annotated, NotRequired

from langchain.agents.middleware import AgentState
from langchain.agents.middleware.types import OmitFromInput
from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.core.enums import ToolKind
from app.services.run_approval_policy import denied_tool_names
from app.services.tool_registry import ToolContext
from app.services.user_tools import load_run_tools, model_tool_name, tool_display_name


def replace_selection(previous: dict, current: dict) -> dict:
    """每次选择替换上一组；并行搜索按图更新顺序保留最后一组，不无限累加。"""
    return current


class McpToolState(AgentState):
    """只允许平台工具写入的检查点状态；不从用户输入接受选择。"""

    mcp_tool_selection: NotRequired[Annotated[dict, replace_selection, OmitFromInput]]


def selected_mcp_tools(state: dict, run_id: str) -> frozenset[str]:
    """只接受当前 Run 的加载选择，跨轮历史不会自动授予 Schema。"""
    selection = state.get("mcp_tool_selection") or {}
    if selection.get("run_id") != run_id:
        return frozenset()
    return frozenset(name for name in selection.get("names", []) if isinstance(name, str))


class ToolSearchInput(BaseModel):
    """限制搜索与返回规模；此上限不是 Agent 可关联工具总数限制。"""

    query: str = Field(
        min_length=1,
        max_length=512,
        description="用途关键词、工具名称、远端函数名或完整模型调用标识；* 列出本轮允许的工具",
        examples=["查询订单", "user_tool_12_search", "*"],
    )
    limit: int = Field(default=5, ge=1, le=10, description="本次最多加载的 MCP 工具数", examples=[1, 5])
    offset: int = Field(default=0, ge=0, le=100000, description="匹配结果偏移；继续搜索下一页会替换当前加载组", examples=[0])


def search_score(query: str, fields: list[str]) -> int:
    """按精确名称、词项和中文双字片段排序，不依赖外部索引或嵌入服务。"""
    normalized = query.strip().casefold()
    if normalized == "*":
        return 1
    if not normalized:
        return 0
    content = " ".join(fields).casefold()
    exact = 10000 if any(normalized == value.casefold() for value in fields[:4]) else 0
    terms = re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]+", normalized)
    score = sum(100 for term in terms if term in content)
    for term in terms:
        if len(term) > 2 and re.fullmatch(r"[\u3400-\u9fff]+", term):
            score += sum(1 for index in range(len(term) - 1) if term[index : index + 2] in content)
    return exact + score


def make_tool_search(context: ToolContext, fixed_tools: dict[str, BaseTool]) -> BaseTool:
    """只持有运行解析后的 MCP 工具对象，搜索不发现远端、不创建新 Tool。"""

    @tool("tool_search", args_schema=ToolSearchInput)
    async def tool_search(query: str, runtime: ToolRuntime, limit: int = 5, offset: int = 0) -> Command:
        """搜索本轮已授权的 MCP 工具，将匹配工具的完整 Schema 加载到下一次模型请求。搜索不执行业务操作。"""
        async with context.sessions() as db:
            await context.require_active(db, enabled=lambda current: True)
            available = await load_run_tools(db, context.run_id, context.owner_id)
            denied = await denied_tool_names(db, context.run_id, fixed_tools)
            matches = []
            for definition, version in available:
                name = model_tool_name(definition)
                if definition.kind != ToolKind.MCP or name not in fixed_tools or name in denied:
                    continue
                display = tool_display_name(definition)
                remote = version.mcp_tool_name or ""
                # 只索引用途和稳定身份，不输出地址、凭据、Headers 或完整 Schema。
                score = search_score(query, [name, definition.name, display, remote, fixed_tools[name].description])
                if score:
                    matches.append(
                        (
                            score,
                            {
                                "name": name,
                                "display_name": display,
                                "tool_id": definition.id,
                                "tool_version": version.version,
                                "connection_id": version.mcp_connection_id,
                                "remote_name": remote,
                                "description": fixed_tools[name].description[:240],
                            },
                        )
                    )
        # 使用新会话重新读取，避免身份映射缓存掩盖搜索期间的撤权或取消。
        async with context.sessions() as db:
            await context.require_active(db, enabled=lambda current: True)
            current_names = {model_tool_name(definition) for definition, _ in await load_run_tools(db, context.run_id, context.owner_id)}
            denied = await denied_tool_names(db, context.run_id, fixed_tools)
        matches = [entry for entry in matches if entry[1]["name"] in current_names and entry[1]["name"] not in denied]
        matches.sort(key=lambda entry: (-entry[0], entry[1]["tool_id"]))
        results = [entry[1] for entry in matches[offset : offset + limit]]
        names = [entry["name"] for entry in results]
        content = json.dumps(
            {
                "results": results,
                "total_matches": len(matches),
                "next_offset": offset + len(results) if offset + len(results) < len(matches) else None,
                "loaded_names": names,
                "guidance": "下一次模型请求将绑定这些工具的完整 Schema；本次替换上一组。先检查参数再调用。"
                "搜索不执行远端操作，不改变授权或审批。请顺序搜索，按需换组；不要自动重做已完成或结果未知的写操作。",
            },
            ensure_ascii=False,
        )
        return Command(
            update={
                "mcp_tool_selection": {
                    "run_id": context.run_id,
                    "names": names,
                },
                "messages": [ToolMessage(content=content, name="tool_search", tool_call_id=runtime.tool_call_id)],
            }
        )

    return tool_search
