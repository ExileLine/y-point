# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 18:00:00
# @Author  : yangyuexiong
# @File    : mcp_directory.py

"""面向使用者的连接摘要，以及所有者维护的指定用户授权。"""

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.enums import ToolKind
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import shanghai_now
from app.models.mcp_connection import McpConnection, McpConnectionShare
from app.models.tool import RunTool, UserTool, UserToolVersion
from app.services.identity import audit
from app.services.mcp_access import connection_share
from app.services.mcp_connections import owned_connection
from app.services.mcp_policy import tool_callability_snapshot
from app.services.user_tools import model_tool_name, tool_display_name


async def connection_directory(db: AsyncSession, actor_id: int) -> list[dict]:
    """摘要只含授权范围内已发布工具，不读取或解密任何连接配置。"""
    granted = select(McpConnectionShare.connection_id).where(
        McpConnectionShare.user_id == actor_id, McpConnectionShare.is_deleted == 0, McpConnectionShare.status == 1
    )
    connections = await db.scalars(
        select(McpConnection)
        .where(McpConnection.is_deleted == 0, McpConnection.status == 1, or_(McpConnection.owner_id == actor_id, McpConnection.id.in_(granted)))
        .order_by(McpConnection.id.desc())
    )
    result = []
    for item in connections:
        owner = item.owner_id == actor_id
        share = None if owner else await connection_share(db, item.id, actor_id)
        tools = []
        rows = await db.execute(
            select(UserTool, UserToolVersion)
            .join(UserToolVersion, (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == UserTool.current_version))
            .where(
                UserTool.owner_id == item.owner_id,
                UserTool.is_deleted == 0,
                UserTool.skill_id.is_(None),
                UserTool.kind == ToolKind.MCP,
                UserToolVersion.mcp_connection_id == item.id,
            )
            .order_by(UserTool.id)
        )
        for tool, version in rows:
            if not owner and (share is None or tool.id not in share.tool_ids):
                continue
            tools.append(
                {
                    "tool_id": tool.id,
                    "display_name": tool_display_name(tool),
                    "model_name": model_tool_name(tool),
                    "tool_version": version.version,
                    **await tool_callability_snapshot(db, tool, version, actor_id),
                }
            )
        result.append(
            {
                "id": item.id,
                "name": item.name,
                "version": item.version,
                "share_version": share.version if share is not None else None,
                "callability_checked_at": shanghai_now().isoformat(),
                "owner_id": item.owner_id,
                "enabled": item.enabled,
                "can_edit": owner,
                "allow_credentials": owner or bool(share and share.allow_credentials),
                "tools": tools,
            }
        )
    return result


def share_output(share: McpConnectionShare, user: Admin) -> dict:
    """共享管理响应只向所有者返回指定用户及权限字段。"""
    return {
        "user_id": user.id,
        "username": user.username,
        "version": share.version,
        "tool_ids": share.tool_ids,
        "allow_credentials": share.allow_credentials,
    }


async def cancel_shared_runs(db: AsyncSession, connection_id: int, user_id: int, tool_ids: list[int]) -> None:
    """撤销摘要、工具子集或凭据借用时取消该用户受影响的活动运行。"""
    from app.services.run_manager import TERMINAL_STATES, request_cancel

    versions = select(UserToolVersion.tool_id).where(UserToolVersion.mcp_connection_id == connection_id, UserToolVersion.tool_id.in_(tool_ids))
    runs = await db.scalars(
        select(AgentRun).where(
            AgentRun.owner_id == user_id,
            AgentRun.state.not_in(TERMINAL_STATES),
            AgentRun.id.in_(select(RunTool.run_id).where(RunTool.tool_id.in_(versions))),
        )
    )
    for run in runs:
        await request_cancel(db, run.id, user_id)


async def save_connection_share(db: AsyncSession, connection_id: int, user_id: int, owner_id: int, body) -> dict:
    """所有者按授权版本整体替换工具子集，新增工具不会自动得到授权。"""
    connection = await owned_connection(db, connection_id, owner_id, lock=True)
    user = await db.get(Admin, user_id)
    if user is None or user.id == owner_id or user.is_deleted != 0 or user.status != 1:
        raise CustomException(status_code=404, custom_code=10002, detail="用户不存在或不可共享")
    share = await db.scalar(select(McpConnectionShare).where(McpConnectionShare.connection_id == connection_id, McpConnectionShare.user_id == user_id))
    active = share is not None and share.is_deleted == 0 and share.status == 1
    if body.expected_version != (share.version if active else 0):
        raise CustomException(status_code=409, custom_code=10005, detail="连接共享授权已变化，请刷新后重试")
    if len(body.tool_ids) != len(set(body.tool_ids)) or any(tool_id <= 0 for tool_id in body.tool_ids):
        raise CustomException(status_code=400, custom_code=10005, detail="授权工具 ID 必须为正数且不能重复")
    pairs = list(
        await db.execute(
            select(UserTool, UserToolVersion)
            .join(UserToolVersion, (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == UserTool.current_version))
            .where(UserTool.id.in_(body.tool_ids), UserTool.owner_id == owner_id, UserTool.is_deleted == 0, UserTool.skill_id.is_(None))
        )
    )
    if len(pairs) != len(body.tool_ids) or any(
        tool.kind != ToolKind.MCP or version.mcp_connection_id != connection.id or not version.verified for tool, version in pairs
    ):
        raise CustomException(status_code=409, custom_code=10005, detail="仅能授权此连接中已发布验证的独立 MCP 工具，请刷新目录")
    if share is None:
        share = McpConnectionShare(connection_id=connection_id, user_id=user_id, version=1, tool_ids=[], allow_credentials=False, is_deleted=0, status=1)
        db.add(share)
    else:
        previous_version = share.version
        removed = set(share.tool_ids) - set(body.tool_ids)
        if share.allow_credentials and not body.allow_credentials:
            removed.update(share.tool_ids)
        if removed:
            await cancel_shared_runs(db, connection_id, user_id, list(removed))
        changed = await db.execute(
            update(McpConnectionShare)
            .where(McpConnectionShare.id == share.id, McpConnectionShare.version == previous_version)
            .values(version=previous_version + 1, tool_ids=sorted(body.tool_ids), allow_credentials=body.allow_credentials, is_deleted=0, status=1)
            .execution_options(synchronize_session=False)
        )
        if changed.rowcount != 1:
            raise CustomException(status_code=409, custom_code=10005, detail="连接共享授权已变化，请刷新后重试")
        await db.refresh(share)
    share.tool_ids = sorted(body.tool_ids)
    share.allow_credentials = body.allow_credentials
    audit(db, "mcp_connection.shared", actor_id=owner_id, target_id=user_id, subject=str(connection_id))
    await db.flush()
    return share_output(share, user)
