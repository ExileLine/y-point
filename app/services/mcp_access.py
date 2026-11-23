# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 18:00:00
# @Author  : yangyuexiong
# @File    : mcp_access.py

"""统一连接摘要、工具子集和凭据借用授权，不向接收者提供连接配置。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ToolKind, Visibility
from app.models.mcp_connection import McpConnection, McpConnectionShare
from app.models.tool import UserTool, UserToolVersion


async def connection_share(db: AsyncSession, connection_id: int, actor_id: int) -> McpConnectionShare | None:
    """读取当前仍存在的指定用户授权；删除后所有执行入口立即失权。"""
    return await db.scalar(
        select(McpConnectionShare).where(
            McpConnectionShare.connection_id == connection_id,
            McpConnectionShare.user_id == actor_id,
            McpConnectionShare.is_deleted == 0,
            McpConnectionShare.status == 1,
        )
    )


async def shared_tool_ids(db: AsyncSession, actor_id: int) -> set[int]:
    """为目录查询收集明确授权的工具 ID，执行前仍须核对实际连接和版本。"""
    shares = await db.scalars(
        select(McpConnectionShare)
        .join(McpConnection, McpConnection.id == McpConnectionShare.connection_id)
        .where(
            McpConnectionShare.user_id == actor_id,
            McpConnectionShare.is_deleted == 0,
            McpConnectionShare.status == 1,
            McpConnection.is_deleted == 0,
            McpConnection.status == 1,
        )
    )
    allowed = {(share.connection_id, tool_id) for share in shares for tool_id in share.tool_ids}
    if not allowed:
        return set()
    pairs = await db.execute(
        select(UserTool, UserToolVersion)
        .join(UserToolVersion, (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == UserTool.current_version))
        .where(UserTool.id.in_([tool_id for _, tool_id in allowed]))
    )
    ids = set()
    for tool, version in pairs:
        if (version.mcp_connection_id, tool.id) in allowed and await tool_share(db, tool, version, actor_id) is not None:
            ids.add(tool.id)
    return ids


async def tool_share(db: AsyncSession, tool: UserTool, version: UserToolVersion | None, actor_id: int) -> McpConnectionShare | None:
    """指定用户授权只覆盖该连接所有者发布的 MCP 工具及其明确子集。"""
    if tool.kind != ToolKind.MCP or version is None or version.mcp_connection_id is None:
        return None
    connection = await db.get(McpConnection, version.mcp_connection_id)
    if connection is None or connection.is_deleted != 0 or connection.status != 1 or connection.owner_id != tool.owner_id:
        return None
    share = await connection_share(db, connection.id, actor_id)
    return share if share and tool.id in share.tool_ids else None


async def can_access_tool(db: AsyncSession, tool: UserTool, version: UserToolVersion | None, actor_id: int) -> bool:
    """工具所有者、公开可见性和明确的连接工具授权是独立的访问来源。"""
    return tool.owner_id == actor_id or tool.visibility == Visibility.PUBLIC or await tool_share(db, tool, version, actor_id) is not None


async def can_borrow_credentials(db: AsyncSession, tool: UserTool, version: UserToolVersion, actor_id: int) -> bool:
    """凭据借用单独校验；指定用户授权不要求向所有公开用户开放凭据。"""
    if tool.owner_id == actor_id or not version.encrypted_credentials:
        return True
    share = await tool_share(db, tool, version, actor_id)
    if share and share.allow_credentials:
        return True
    if tool.visibility != Visibility.PUBLIC:
        return False
    current = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version))
    if current is None or not current.share_credentials:
        return False
    if version.mcp_connection_id is None:
        return True
    connection = await db.get(McpConnection, version.mcp_connection_id)
    return bool(connection and connection.share_credentials)


async def require_group_access(db: AsyncSession, connection_id: int, tool_id: int, actor_id: int) -> None:
    """连接来源必须仍获明确授权；共享 Agent 不转授连接或工具子集。"""
    from app.core.custom_exception import CustomException

    connection = await db.get(McpConnection, connection_id)
    if connection is not None and connection.is_deleted == 0 and connection.status == 1 and connection.enabled:
        if connection.owner_id == actor_id:
            return
        share = await connection_share(db, connection_id, actor_id)
        if share and tool_id in share.tool_ids:
            return
    raise CustomException(
        status_code=409,
        custom_code=10005,
        detail=f"MCP 连接 #{connection_id} 的工具 #{tool_id} 未授权或已停用，请连接所有者授权该工具子集；共享 Agent 不包含连接权限",
    )
