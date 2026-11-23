# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 15:00:00
# @Author  : yangyuexiong
# @File    : mcp_sync.py

"""远端差异预览、显式发布与 Agent 版本升级，不自动扩大工具授权。"""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.core.custom_exception import CustomException
from app.core.enums import (
    McpOperationKind,
    McpOperationStatus,
    RecoveryPolicy,
    ToolApprovalMode,
    McpSyncAction,
    McpSyncState,
    ToolCallability,
    ToolBlockReason,
    ToolEffectSource,
    ToolKind,
    Visibility,
)
from app.models.agent_config import AgentConfig
from app.models.mcp_connection import McpConnection, McpOperation
from app.models.mcp_connection import AgentMcpTool
from app.models.tool import AgentTool, UserTool, UserToolVersion
from app.services.identity import audit
from app.services.mcp_connections import bind_connection
from app.services.mcp_policy import cancel_tightened_tool_runs, prepare_mcp_effect, tool_callability
from app.services.user_tools import cancel_tool_runs, model_tool_name, validate_tool_definition, version_fields


def digest(value: object) -> str:
    """生成确定性摘要，敏感配置只参与摘要、不进入响应。"""
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


async def replay_operation(db: AsyncSession, owner_id: int, kind: str, target_id: int, body) -> dict | None:
    """在网络访问和版本校验前处理已完成请求，相同 ID 不接受不同内容。"""
    receipt = await db.scalar(
        select(McpOperation).where(
            McpOperation.owner_id == owner_id,
            McpOperation.kind == kind,
            McpOperation.target_id == target_id,
            McpOperation.request_id == body.request_id,
        )
    )
    if receipt is None:
        return None
    if receipt.input_hash != digest(body.model_dump(mode="json")):
        raise CustomException(status_code=409, custom_code=10005, detail="操作 ID 已用于不同请求，请使用新的请求 ID")
    return receipt.result


def remember_operation(db: AsyncSession, owner_id: int, kind: str, target_id: int, body, result: dict) -> None:
    """回执与业务变更在同一事务提交，中断重试不会重复发布。"""
    db.add(
        McpOperation(
            owner_id=owner_id, kind=kind, target_id=target_id, request_id=body.request_id, input_hash=digest(body.model_dump(mode="json")), result=result
        )
    )
    audit(db, f"mcp.{kind}", actor_id=owner_id, target_id=target_id)


async def connection_preview(db: AsyncSession, connection: McpConnection, remote: list[dict]) -> dict:
    """比较当前发布版本、远端定义与连接快照，返回无秘密的逐项差异。"""
    versions = (
        await db.execute(
            select(UserTool, UserToolVersion)
            .join(
                UserToolVersion,
                (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == UserTool.current_version),
            )
            .where(
                UserTool.owner_id == connection.owner_id,
                UserTool.is_deleted == 0,
                UserTool.kind == ToolKind.MCP,
                UserTool.skill_id.is_(None),
                UserToolVersion.mcp_connection_id == connection.id,
            )
            .order_by(UserTool.id)
        )
    ).all()
    remote_by_name = {item["name"]: item for item in remote}
    rows = []
    bound = set()
    for tool, version in versions:
        name = version.mcp_tool_name
        bound.add(name)
        found = remote_by_name.get(name)
        changes = []
        if found is not None:
            if version.input_schema != found["input_schema"]:
                changes.append("schema")
            if (version.mcp_annotations or {}) != (found.get("annotations") or {}):
                changes.append("annotations")
            if version.mcp_config != connection.config or version.encrypted_credentials != connection.encrypted_credentials:
                changes.append("execution_config")
            if version.share_credentials != connection.share_credentials:
                changes.append("credential_sharing")
            if (version.mcp_connection_version or 0) <= (connection.config_revoked_through or 0) or (
                version.encrypted_credentials and (version.mcp_connection_version or 0) <= connection.credentials_revoked_through
            ):
                changes.append("revoked_config")
            if not tool.enabled:
                changes.append("disabled")
        state = McpSyncState.REMOVED if found is None else McpSyncState.CHANGED if changes else McpSyncState.UNCHANGED
        rows.append(
            {
                "tool_id": tool.id,
                "remote_name": name,
                "display_name": tool.display_name or tool.name,
                "model_name": model_tool_name(tool),
                "current_version": version.version,
                "state": state,
                "changes": changes,
                "previous_schema": version.input_schema,
                "schema": found["input_schema"] if found else None,
                "previous_annotations": version.mcp_annotations or {},
                "annotations": found.get("annotations", {}) if found else {},
                "requires_read_confirmation": bool(changes and version.effect_source == ToolEffectSource.OWNER_READ),
                "requires_idempotency_verification": version.recovery_policy == RecoveryPolicy.IDEMPOTENT,
                "approval_mode": version.approval_mode or (ToolApprovalMode.ALWAYS if version.requires_approval else ToolApprovalMode.INHERIT),
            }
        )
    for name, found in remote_by_name.items():
        if name not in bound:
            rows.append(
                {
                    "tool_id": None,
                    "remote_name": name,
                    "display_name": name,
                    "model_name": None,
                    "current_version": None,
                    "state": McpSyncState.ADDED,
                    "changes": [],
                    "previous_schema": None,
                    "schema": found["input_schema"],
                    "previous_annotations": {},
                    "annotations": found.get("annotations", {}),
                    "requires_read_confirmation": False,
                    "requires_idempotency_verification": False,
                    "approval_mode": ToolApprovalMode.INHERIT,
                }
            )
    return {
        "connection_id": connection.id,
        "connection_version": connection.version,
        "items": rows,
        "preview_hash": digest([connection.id, connection.version, rows, remote, digest(connection.config), digest(connection.encrypted_credentials)]),
    }


async def publish_selection(db: AsyncSession, connection: McpConnection, selection, remote: dict | None) -> dict:
    """在单项保存点发布不可变新版本，保留旧 Agent 与 Run 的绑定。"""
    tool = None
    previous = None
    if selection.tool_id is not None:
        tool = await db.get(UserTool, selection.tool_id)
        if tool is None or tool.owner_id != connection.owner_id or tool.is_deleted != 0 or tool.kind != ToolKind.MCP:
            raise ValueError("工具不存在或无权发布")
        previous = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version))
        if previous is None or previous.mcp_connection_id != connection.id or previous.mcp_tool_name != selection.remote_name:
            raise ValueError("工具绑定已变化，请重新预览")
        if tool.current_version != selection.expected_version:
            raise ValueError("工具版本已变化，请重新预览")
    elif selection.expected_version is not None:
        raise ValueError("新工具不能指定历史版本")
    if selection.action == McpSyncAction.DISABLE:
        if tool is None or remote is not None:
            raise ValueError("仅能停用预览中已移除的已有工具")
        locked = await db.execute(update(UserTool).where(UserTool.id == tool.id, UserTool.current_version == selection.expected_version).values(enabled=False))
        if locked.rowcount != 1:
            raise ValueError("工具版本已变化")
        await cancel_tool_runs(db, tool)
        return {
            "tool_id": tool.id,
            "version": tool.current_version,
            "status": McpOperationStatus.DISABLED,
        }
    if remote is None:
        raise ValueError("远端函数已移除，无法发布")
    if tool is None:
        display = selection.display_name or f"{connection.name} · {selection.remote_name}"[:110]
        if not selection.display_name and await db.scalar(
            select(UserTool.id).where(UserTool.owner_id == connection.owner_id, UserTool.display_name == display)
        ):
            display = f"{display} {uuid4().hex[:8]}"
        tool = UserTool(
            owner_id=connection.owner_id,
            name=f"tool_{uuid4().hex[:16]}",
            display_name=display,
            description=(remote.get("description") or selection.remote_name)[:1024],
            kind=ToolKind.MCP,
            visibility=Visibility.PRIVATE,
            enabled=True,
            current_version=1,
        )
        db.add(tool)
        await db.flush()
    fields = version_fields(
        {
            "visibility": tool.visibility,
            "mcp_connection_id": connection.id,
            "mcp_tool_name": selection.remote_name,
            **selection.model_dump(exclude_none=True, include={"effect", "recovery_policy", "read_only_reason"}),
        },
        previous,
    )
    await bind_connection(db, fields, connection.owner_id)
    prepare_mcp_effect(fields, remote, connection.owner_id, previous)
    effect, _, approval, definition_hash = validate_tool_definition(ToolKind.MCP, tool.name, fields)
    for key in ("visibility", "effect", "requires_approval"):
        fields.pop(key, None)
    number = previous.version + 1 if previous else 1
    if previous is not None:
        updated = await db.execute(
            update(UserTool)
            .where(UserTool.id == tool.id, UserTool.current_version == previous.version, UserTool.is_deleted == 0)
            .values(current_version=number)
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            raise ValueError("工具版本已变化，请重新预览")
        await db.refresh(tool)
    db.add(UserToolVersion(tool_id=tool.id, version=number, content_hash=definition_hash, effect=effect, requires_approval=approval, verified=True, **fields))
    await db.flush()
    await cancel_tightened_tool_runs(db, tool_id=tool.id)
    return {
        "tool_id": tool.id,
        "version": number,
        "status": McpOperationStatus.PUBLISHED,
    }


async def apply_connection_sync(db: AsyncSession, connection: McpConnection, body, remote: list[dict]) -> dict:
    """重新发现后核对预览与连接 CAS，逐项结果和回执原子提交。"""
    preview = await connection_preview(db, connection, remote)
    if connection.version != body.expected_version or preview["preview_hash"] != body.preview_hash:
        raise CustomException(status_code=409, custom_code=10005, detail="连接、远端或本地工具已变化，请重新预览")
    keys = [(item.tool_id, item.remote_name) for item in body.selections]
    if len(keys) != len(set(keys)):
        raise CustomException(status_code=400, custom_code=10005, detail="不能重复选择相同绑定")
    allowed = {(row["tool_id"], row["remote_name"]): row for row in preview["items"]}
    if any(key not in allowed for key in keys):
        raise CustomException(status_code=409, custom_code=10005, detail="选择项不在当前预览中")
    lock = await db.execute(
        update(McpConnection)
        .where(
            McpConnection.id == connection.id, McpConnection.version == body.expected_version, McpConnection.enabled.is_(True), McpConnection.is_deleted == 0
        )
        .values(version=body.expected_version)
    )
    if lock.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="连接版本已变化，请重新预览")
    by_name = {item["name"]: item for item in remote}
    results = []
    for selection in body.selections:
        try:
            async with db.begin_nested():
                result = await publish_selection(db, connection, selection, by_name.get(selection.remote_name))
        except (ValueError, CustomException, IntegrityError) as exc:
            result = {
                "tool_id": selection.tool_id,
                "status": McpOperationStatus.FAILED,
                "message": str(exc) if isinstance(exc, ValueError) else exc.detail if isinstance(exc, CustomException) else "显示名称冲突，请更换名称后重试",
            }
        results.append(
            {
                "remote_name": selection.remote_name,
                **result,
            }
        )
    result = {
        "request_id": body.request_id,
        "items": results,
    }
    remember_operation(db, connection.owner_id, McpOperationKind.CONNECTION_SYNC, connection.id, body, result)
    return result


async def agent_upgrade_preview(db: AsyncSession, agent: AgentConfig) -> dict:
    """只读取已发布版本，展示授权、Schema 和有效审批变化，不访问私有连接配置。"""
    from app.services.user_tools import agent_tool_rows
    from app.services.mcp_access import can_access_tool

    bindings = await agent_tool_rows(db, agent.id)
    grouped = list(await db.scalars(select(AgentMcpTool).where(AgentMcpTool.agent_id == agent.id)))
    rows = []
    for binding in bindings:
        tool = await db.get(UserTool, binding.tool_id)
        if tool is None:
            continue
        previous = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == binding.tool_version))
        latest = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version))
        decision = await tool_callability(db, tool, latest, agent.owner_id)
        for group in grouped:
            if group.tool_id != binding.tool_id:
                continue
            from app.services.mcp_access import require_group_access

            try:
                await require_group_access(db, group.connection_id, group.tool_id, agent.owner_id)
                if latest is None or latest.mcp_connection_id != group.connection_id:
                    raise CustomException(status_code=409, custom_code=10005, detail="工具已改绑连接，请移除旧连接选择并重新关联")
            except CustomException as exc:
                decision = {
                    "status": ToolCallability.BLOCKED,
                    "reason": ToolBlockReason.CONNECTION_UNAVAILABLE,
                    "message": exc.detail,
                    "action": "请修复连接授权或重新关联工具",
                    "policy": None,
                }
        authorized = tool.is_deleted == 0 and await can_access_tool(db, tool, latest, agent.owner_id)
        rows.append(
            {
                "tool_id": tool.id,
                "display_name": tool.display_name or tool.name if authorized else "工具授权已失效",
                "model_name": model_tool_name(tool) if authorized else None,
                "current_version": binding.tool_version,
                "target_version": tool.current_version,
                "changed": binding.tool_version != tool.current_version,
                "callability": decision,
                "previous_schema": previous.input_schema if previous and authorized else None,
                "schema": latest.input_schema if latest and authorized else None,
                "changes": [
                    key
                    for key in ("input_schema", "effect", "approval_mode", "mcp_annotations", "share_credentials", "mcp_config")
                    if previous and latest and getattr(previous, key) != getattr(latest, key)
                ]
                if authorized
                else ["access"],
                "requires_read_confirmation": bool(latest and latest.effect_source == ToolEffectSource.OWNER_READ) if authorized else False,
            }
        )
    return {
        "agent_id": agent.id,
        "agent_version": agent.version,
        "items": rows,
        "preview_hash": digest([agent.id, agent.version, rows]),
    }


async def apply_agent_upgrades(db: AsyncSession, agent: AgentConfig, body) -> dict:
    """仅升级已选绑定，保留旧 Run 快照，Agent CAS 防止覆盖并发编辑。"""
    preview = await agent_upgrade_preview(db, agent)
    if agent.version != body.expected_version or preview["preview_hash"] != body.preview_hash:
        raise CustomException(status_code=409, custom_code=10005, detail="Agent 或工具状态已变化，请重新预览")
    if len({s.tool_id for s in body.selections}) != len(body.selections):
        raise CustomException(status_code=400, custom_code=10005, detail="升级工具不能重复")
    updated = await db.execute(
        update(AgentConfig)
        .where(AgentConfig.id == agent.id, AgentConfig.version == body.expected_version, AgentConfig.is_deleted == 0)
        .values(version=body.expected_version + 1)
    )
    if updated.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="Agent 配置版本已变化")
    by_id = {row["tool_id"]: row for row in preview["items"]}
    results = []
    for selection in body.selections:
        row = by_id.get(selection.tool_id)
        if (
            row is None
            or row["current_version"] != selection.expected_version
            or row["target_version"] != selection.target_version
            or row["callability"]["status"] == ToolCallability.BLOCKED
        ):
            results.append(
                {
                    "tool_id": selection.tool_id,
                    "status": McpOperationStatus.FAILED,
                    "message": "工具授权、状态或版本不符合预览，请修复后重新选择",
                }
            )
            continue
        await db.execute(
            update(AgentTool)
            .where(AgentTool.agent_id == agent.id, AgentTool.tool_id == selection.tool_id, AgentTool.tool_version == selection.expected_version)
            .values(tool_version=selection.target_version)
        )
        await db.execute(
            update(AgentMcpTool)
            .where(AgentMcpTool.agent_id == agent.id, AgentMcpTool.tool_id == selection.tool_id, AgentMcpTool.tool_version == selection.expected_version)
            .values(tool_version=selection.target_version)
        )
        results.append(
            {
                "tool_id": selection.tool_id,
                "status": McpOperationStatus.UPGRADED,
                "version": selection.target_version,
            }
        )
    result = {
        "request_id": body.request_id,
        "agent_version": body.expected_version + 1,
        "items": results,
    }
    remember_operation(db, agent.owner_id, McpOperationKind.AGENT_UPGRADE, agent.id, body, result)
    return result
