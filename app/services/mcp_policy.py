# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 10:00:00
# @Author  : yangyuexiong
# @File    : mcp_policy.py

"""统一计算 MCP 审批与元数据可调用状态，不将执行许可等同于可安全恢复。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.enums import (
    RecoveryPolicy,
    McpApprovalConstraint,
    RunState,
    ToolApprovalMode,
    ToolBlockReason,
    ToolCallability,
    ToolEffect,
    ToolEffectSource,
    ToolEnvironmentState,
    ToolKind,
    Visibility,
)
from app.models.agent_run import AgentRun
from app.models.base import shanghai_now
from app.models.mcp_connection import McpConnection
from app.models.tool import RunTool, UserTool, UserToolVersion


def approval_policy(
    tool: UserTool,
    version: UserToolVersion,
    actor_id: int,
    *,
    connection: McpConnection | None = None,
    policy_version: UserToolVersion | None = None,
    require_all: bool = False,
) -> dict:
    """所有者覆盖后再应用使用者与部署约束；动作性质始终取固定执行版本。"""
    policy_version = policy_version or version
    if tool.kind != ToolKind.MCP:
        return {
            "schema_version": 1,
            "mode": ToolApprovalMode.ALWAYS if policy_version.requires_approval else ToolApprovalMode.NEVER,
            "source": "tool",
            "requires_approval": bool(policy_version.requires_approval),
            "constraints": [],
        }
    override = policy_version.approval_mode
    if override is None:
        override = ToolApprovalMode.ALWAYS if policy_version.requires_approval else ToolApprovalMode.INHERIT
    if override == ToolApprovalMode.INHERIT:
        mode = connection.approval_mode if connection is not None else ToolApprovalMode.WRITES
        source = "connection" if connection is not None else "default"
    else:
        mode, source = override, "tool"
    # 无效历史配置按每次审批处理，不能因新字段缺损而扩大执行许可。
    if mode not in (ToolApprovalMode.ALWAYS, ToolApprovalMode.WRITES, ToolApprovalMode.NEVER):
        mode, source = ToolApprovalMode.ALWAYS, "invalid_policy"
    readonly = version.effect == ToolEffect.READ
    required = mode == ToolApprovalMode.ALWAYS or mode == ToolApprovalMode.WRITES and not readonly
    constraints = []
    if actor_id != tool.owner_id and not readonly:
        required = True
        constraints.append(McpApprovalConstraint.UNTRUSTED_TOOL)
    if actor_id != tool.owner_id and readonly and version.effect_source != ToolEffectSource.REMOTE_READ:
        required = True
        constraints.append(McpApprovalConstraint.UNTRUSTED_READ)
    if require_all:
        required = True
        constraints.append(McpApprovalConstraint.CALLER)
    if get_config().MCP_FORCE_APPROVAL:
        required = True
        constraints.append(McpApprovalConstraint.DEPLOYMENT)
    return {
        "schema_version": 1,
        "mode": mode,
        "source": source,
        "tool_policy_version": policy_version.version,
        "connection_policy_version": connection.version if connection is not None else None,
        "effect_source": version.effect_source or ToolEffectSource.LEGACY,
        "requires_approval": bool(required),
        "constraints": constraints,
    }


async def tool_callability(
    db: AsyncSession,
    tool: UserTool,
    version: UserToolVersion | None,
    actor_id: int,
    *,
    policy_version: UserToolVersion | None = None,
    require_all: bool = False,
) -> dict:
    """预检当前授权和配置，不探测远端在线状态，也不返回私有连接配置。"""

    def blocked(reason: ToolBlockReason, message: str, action: str) -> dict:
        """生成不携带凭据或内部地址的稳定阻断反馈。"""
        return {
            "status": ToolCallability.BLOCKED,
            "reason": reason,
            "message": message,
            "action": action,
            "policy": None,
        }

    from app.services.mcp_access import can_access_tool, can_borrow_credentials

    if tool.is_deleted != 0 or tool.status != 1 or not await can_access_tool(db, tool, version, actor_id):
        return blocked(ToolBlockReason.ACCESS_DENIED, "当前用户无权使用此工具", "请所有者授权或选择其他工具")
    if not tool.enabled:
        return blocked(ToolBlockReason.DISABLED, "工具已停用", "请所有者启用工具")
    if version is None or version.is_deleted != 0 or version.status != 1:
        return blocked(ToolBlockReason.VERSION_UNAVAILABLE, "执行版本不可用", "刷新并选择已发布版本")
    if not version.verified:
        return blocked(ToolBlockReason.UNVERIFIED, "工具尚未完成验证", "请所有者先完成试运行")
    if version.encrypted_credentials and version.version <= (tool.credentials_revoked_through or 0):
        return blocked(ToolBlockReason.CREDENTIALS_REVOKED, "此工具版本的凭据已撤销", "替换凭据并重新发布工具")
    if policy_version is None:
        policy_version = await db.scalar(
            select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version),
        )
    if policy_version is None:
        return blocked(ToolBlockReason.VERSION_UNAVAILABLE, "当前发布策略不可用", "请所有者检查发布版本")
    if not await can_borrow_credentials(db, tool, version, actor_id):
        return blocked(ToolBlockReason.CREDENTIALS_NOT_SHARED, "所有者未授权使用连接凭据", "请所有者授权凭据借用")
    connection = None
    if tool.kind == ToolKind.MCP and version.mcp_connection_id is not None:
        connection = await db.get(McpConnection, version.mcp_connection_id)
        if connection is None or connection.is_deleted != 0 or connection.status != 1 or connection.owner_id != tool.owner_id:
            return blocked(ToolBlockReason.CONNECTION_UNAVAILABLE, "MCP 连接授权已失效", "请所有者检查连接并重新发布")
        if not connection.enabled:
            return blocked(ToolBlockReason.CONNECTION_DISABLED, "MCP 连接已停用", "请所有者启用连接")
        if (version.mcp_connection_version or 0) <= (connection.config_revoked_through or 0):
            return blocked(ToolBlockReason.CREDENTIALS_REVOKED, "连接数据范围配置已撤销", "按当前连接重新发布工具")
        if version.encrypted_credentials and (version.mcp_connection_version or 0) <= connection.credentials_revoked_through:
            return blocked(ToolBlockReason.CREDENTIALS_REVOKED, "连接凭据已轮换，此版本已撤销", "使用新凭据重新发布工具")
    if tool.kind == ToolKind.MCP and version.effect != ToolEffect.READ and version.recovery_policy == RecoveryPolicy.IDEMPOTENT:
        if not version.idempotency_key_field or not version.write_contract_verified:
            return blocked(ToolBlockReason.IDEMPOTENCY_UNVERIFIED, "声明的幂等恢复契约尚未验收", "完成契约验收，或改用 manual 恢复策略")
    if tool.kind == ToolKind.SCRIPT and version.requirements and version.environment_state != ToolEnvironmentState.READY:
        return blocked(ToolBlockReason.ENVIRONMENT_NOT_READY, "脚本依赖环境未就绪", "查看依赖准备进度或重试")
    policy = approval_policy(tool, version, actor_id, connection=connection, policy_version=policy_version, require_all=require_all)
    return {
        "status": ToolCallability.APPROVAL_REQUIRED if policy["requires_approval"] else ToolCallability.DIRECT,
        "reason": None,
        "message": "审批后可调用" if policy["requires_approval"] else "可直接调用",
        "action": "在对话中确认本次操作" if policy["requires_approval"] else None,
        "policy": policy,
    }


async def tool_callability_snapshot(db: AsyncSession, tool: UserTool, version: UserToolVersion | None, actor_id: int) -> dict:
    """为管理入口提供同源预检、服务端判断时间及安全的固定连接版本摘要。"""
    decision = await tool_callability(db, tool, version, actor_id)
    return {
        "callability": decision,
        "callability_checked_at": shanghai_now().isoformat(),
        "mcp_connection_version": version.mcp_connection_version if version is not None else None,
    }


async def cancel_tightened_tool_runs(db: AsyncSession, *, tool_id: int | None = None, connection_id: int | None = None) -> None:
    """策略收紧后仅取消原先未要求审批的相关运行；已要求审批的快照继续有效。"""
    from app.services.run_manager import request_cancel

    query = (
        select(AgentRun, RunTool, UserTool, UserToolVersion)
        .select_from(AgentRun)
        .join(RunTool, RunTool.run_id == AgentRun.id)
        .join(UserTool, UserTool.id == RunTool.tool_id)
        .join(UserToolVersion, (UserToolVersion.tool_id == RunTool.tool_id) & (UserToolVersion.version == RunTool.tool_version))
        .where(
            UserTool.kind == ToolKind.MCP,
            AgentRun.state.in_([RunState.QUEUED, RunState.RUNNING, RunState.WAITING_FOR_APPROVAL]),
        )
    )
    if tool_id is not None:
        query = query.where(UserTool.id == tool_id)
    if connection_id is not None:
        query = query.where(UserToolVersion.mcp_connection_id == connection_id)
    for run, fixed, tool, version in (await db.execute(query)).all():
        if fixed.requires_approval is True or fixed.requires_approval is None and version.requires_approval:
            continue
        decision = await tool_callability(db, tool, version, run.owner_id, require_all=run.require_mcp_approval)
        if decision["policy"] and decision["policy"]["requires_approval"]:
            await request_cancel(db, run.id, run.owner_id)


def prepare_mcp_effect(fields: dict, remote: dict, owner_id: int, previous: UserToolVersion | None = None) -> None:
    """按远端显式提示或所有者确认推导性质，只读确认绑定实际执行定义。"""
    import hashlib
    import json

    from app.models.base import shanghai_now

    fields["input_schema"] = remote["input_schema"]
    fields["mcp_annotations"] = dict(remote.get("annotations") or {})
    hint = remote.get("read_only")
    if hint is not None:
        fields["mcp_annotations"]["readOnlyHint"] = hint
    fingerprint = hashlib.sha256(
        json.dumps(
            {key: fields.get(key) for key in ("input_schema", "mcp_annotations", "mcp_config", "mcp_url", "mcp_tool_name", "encrypted_credentials")},
            sort_keys=True,
            ensure_ascii=False,
        ).encode(),
    ).hexdigest()
    confirmation = previous.read_confirmation if previous is not None else None
    fields["read_confirmation"] = None
    effect_explicit = fields.pop("effect_explicit", False)
    explicit_write = fields.get("effect") == ToolEffect.WRITE and (
        effect_explicit or previous is not None and previous.effect_source == ToolEffectSource.OWNER_WRITE
    )
    if hint is True:
        fields["effect"] = ToolEffect.WRITE if explicit_write else ToolEffect.READ
        fields["effect_source"] = ToolEffectSource.OWNER_WRITE if explicit_write else ToolEffectSource.REMOTE_READ
    elif hint is False:
        if fields.get("effect") == ToolEffect.READ:
            raise ValueError("远端明确声明非只读，不能通过人工确认降级；请按写操作发布")
        fields["effect"] = ToolEffect.WRITE
        fields["effect_source"] = ToolEffectSource.REMOTE_WRITE
    elif fields.get("effect") == ToolEffect.READ:
        reason = (fields.get("read_only_reason") or "").strip()
        if confirmation and confirmation.get("definition_hash") == fingerprint and not reason:
            fields["read_confirmation"] = dict(confirmation)
        elif reason and fields.get("visibility") == Visibility.PRIVATE:
            fields["read_confirmation"] = {
                "actor_id": owner_id,
                "reason": reason,
                "definition_hash": fingerprint,
                "confirmed_at": shanghai_now().isoformat(),
            }
        else:
            raise ValueError("远端未声明只读：请在私有工具中填写只读确认理由；执行定义变化后需重新确认")
        fields["effect"] = ToolEffect.READ
        fields["effect_source"] = ToolEffectSource.OWNER_READ
    else:
        fields["effect"] = ToolEffect.WRITE
        fields["effect_source"] = ToolEffectSource.UNKNOWN
    if fields["effect"] == ToolEffect.READ:
        fields["recovery_policy"] = RecoveryPolicy.SAFE
        fields["idempotency_key_field"] = None
    elif fields.get("recovery_policy") == RecoveryPolicy.SAFE:
        fields["recovery_policy"] = RecoveryPolicy.MANUAL
    # 保留既有逐版本验收规则，不能把旧版本证据自动当作新版本已经验收。
    fields["write_contract_verified"] = False
    fields["write_contract_evidence_hash"] = None
