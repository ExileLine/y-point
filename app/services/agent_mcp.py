# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 18:00:00
# @Author  : yangyuexiong
# @File    : agent_mcp.py

"""Agent 连接选择固定为已发布工具版本，统一重复和冲突处理。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.enums import ToolCallability
from app.models.mcp_connection import AgentMcpTool, McpConnection
from app.models.tool import UserTool, UserToolVersion
from app.services.mcp_access import connection_share, require_group_access
from app.services.mcp_policy import tool_callability


def binding_conflict(tool_id: int) -> CustomException:
    """提示用户统一直接选择和连接选择的固定版本。"""
    return CustomException(
        status_code=409, custom_code=10005, detail=f"工具 #{tool_id} 在直接选择或连接选择中版本冲突，请统一版本后重试；升级请使用工具版本升级入口"
    )


async def connection_bindings(db: AsyncSession, agent_id: int) -> list[AgentMcpTool]:
    """读取 Agent 明确固定的连接工具子集。"""
    return list(await db.scalars(select(AgentMcpTool).where(AgentMcpTool.agent_id == agent_id).order_by(AgentMcpTool.id)))


async def connection_selection_output(db: AsyncSession, agent_id: int) -> list[dict]:
    """按连接输出选择，保持旧客户端 tool_ids 仅包含直接关联。"""
    groups = {}
    for row in await connection_bindings(db, agent_id):
        groups.setdefault(row.connection_id, []).append(
            {
                "tool_id": row.tool_id,
                "tool_version": row.tool_version,
            }
        )
    return [
        {
            "connection_id": connection_id,
            "tools": tools,
        }
        for connection_id, tools in groups.items()
    ]


async def validate_connection_selection(db: AsyncSession, selections: list[dict], actor_id: int, *, existing: list | None = None) -> list[dict]:
    """保存前验证摘要可见、工具属于连接、版本已发布以及实际使用权限。"""
    seen_connections = set()
    seen_tools = set()
    old = {(row.connection_id, row.tool_id): row.tool_version for row in existing or []}
    bindings = []
    for group in selections:
        connection_id = group["connection_id"]
        if connection_id in seen_connections:
            raise CustomException(status_code=400, custom_code=10005, detail="连接选择不能重复")
        seen_connections.add(connection_id)
        connection = await db.get(McpConnection, connection_id)
        if (
            connection is None
            or connection.is_deleted != 0
            or connection.status != 1
            or (connection.owner_id != actor_id and await connection_share(db, connection_id, actor_id) is None)
        ):
            raise CustomException(status_code=404, custom_code=10002, detail=f"MCP 连接 #{connection_id} 未授权，请所有者共享连接及工具子集")
        for selected in group["tools"]:
            tool_id, number = selected["tool_id"], selected["tool_version"]
            await require_group_access(db, connection_id, tool_id, actor_id)
            if tool_id in seen_tools:
                raise CustomException(status_code=400, custom_code=10005, detail="连接选择中的工具不能重复")
            seen_tools.add(tool_id)
            tool = await db.get(UserTool, tool_id)
            version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == number))
            if tool is None or tool.skill_id is not None or version is None or version.mcp_connection_id != connection_id:
                raise CustomException(status_code=409, custom_code=10005, detail=f"工具 #{tool_id} 不属于所选连接或版本不可用，请刷新目录")
            if (connection_id, tool_id) in old and old[connection_id, tool_id] != number:
                raise binding_conflict(tool_id)
            decision = await tool_callability(db, tool, version, actor_id)
            if decision["status"] == ToolCallability.BLOCKED:
                raise CustomException(
                    status_code=409, custom_code=10005, detail=f"连接 #{connection_id} 工具 #{tool_id}：{decision['message']}；{decision['action']}"
                )
            bindings.append(
                {
                    "connection_id": connection_id,
                    "tool_id": tool_id,
                    "tool_version": number,
                }
            )
    return bindings


def validate_binding_versions(direct: dict[int, int], grouped: list) -> None:
    """相同工具同版本去重，不同版本拒绝，避免顺序决定权限。"""
    versions = dict(direct)
    for row in grouped:
        tool_id = row["tool_id"] if isinstance(row, dict) else row.tool_id
        number = row["tool_version"] if isinstance(row, dict) else row.tool_version
        if tool_id in versions and versions[tool_id] != number:
            raise binding_conflict(tool_id)
        versions[tool_id] = number
