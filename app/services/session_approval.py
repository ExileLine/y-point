# -*- coding: utf-8 -*-
# @Time    : 2026-11-25 08:54:59
# @Author  : yangyuexiong
# @File    : session_approval.py

"""会话内 MCP 放行只绑定已授权工具版本，不能覆盖部署、Run 或共享约束。"""

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.enums import RunEventType, SessionApprovalMode, SessionApprovalScope, ToolCallability, ToolKind
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_session import AgentSession
from app.models.run_approval import RunApproval
from app.models.tool import RunTool, UserTool, UserToolVersion
from app.services.identity import audit
from app.services.mcp_policy import tool_callability
from app.services.user_tools import model_tool_name, tool_display_name


async def _candidates(db: AsyncSession, run: AgentRun) -> dict[str, dict]:
    """从固定执行版本和当前审批配置生成可授予的所有者工具身份。"""
    if run.require_mcp_approval or get_config().MCP_FORCE_APPROVAL:
        return {}
    rows = (
        await db.execute(
            select(UserTool, UserToolVersion)
            .join(RunTool, RunTool.tool_id == UserTool.id)
            .join(UserToolVersion, (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == RunTool.tool_version))
            .where(RunTool.run_id == run.id, UserTool.kind == ToolKind.MCP, UserTool.owner_id == run.owner_id)
        )
    ).all()
    result = {}
    for tool, version in rows:
        current = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version))
        if current is None:
            continue
        decision = await tool_callability(db, tool, version, run.owner_id, policy_version=current, require_all=run.require_mcp_approval)
        if decision["status"] == ToolCallability.BLOCKED or decision["policy"]["constraints"] or decision["policy"]["source"] == "invalid_policy":
            continue
        result[model_tool_name(tool)] = {
            "tool_id": tool.id,
            "tool_version": version.version,
            "content_hash": version.content_hash,
            "policy_version": current.version,
            "connection_id": version.mcp_connection_id,
            "connection_version": decision["policy"]["connection_policy_version"],
            "name": tool_display_name(tool),
        }
    return result


async def allowed_session_tools(db: AsyncSession, run: AgentRun) -> dict[str, dict]:
    """重新核对会话、工具及策略版本；旧放行不授予工具或凭据使用权。"""
    if run.session_id is None:
        return {}
    session = await db.get(AgentSession, run.session_id, populate_existing=True)
    if session is None or session.owner_id != run.owner_id or session.agent_id != run.agent_id or session.is_deleted != 0 or session.archived:
        return {}
    policy = session.approval_policy or {}
    if policy.get("mode") not in (SessionApprovalMode.ALLOW_TOOLS, SessionApprovalMode.ALLOW_ALL):
        return {}
    if policy["mode"] == SessionApprovalMode.ALLOW_ALL and not get_config().MCP_SESSION_ALLOW_ALL:
        return {}
    saved = policy.get("tools", [])
    return {name: token for name, token in (await _candidates(db, run)).items() if token in saved}


async def prepare_session_grant(db: AsyncSession, run: AgentRun, approval: RunApproval, scope: SessionApprovalScope) -> dict:
    """审批之前验证可放行集合，混合内置动作、他人工具和硬约束均不扩大授权。"""
    if scope == SessionApprovalScope.ONCE:
        return {}
    session = await db.get(AgentSession, run.session_id, populate_existing=True) if run.session_id else None
    if session is None or session.owner_id != run.owner_id or session.agent_id != run.agent_id or session.is_deleted != 0 or session.archived:
        raise ValueError("当前运行没有可授权的活动会话")
    if scope == SessionApprovalScope.ALL and not get_config().MCP_SESSION_ALLOW_ALL:
        raise ValueError("部署不允许本对话全部允许")
    candidates = await _candidates(db, run)
    actions = approval.actions
    names = {action["name"] for action in actions}
    if not actions or any(
        action["name"] not in candidates
        or action.get("tool_id") != candidates[action["name"]]["tool_id"]
        or action.get("tool_version") != candidates[action["name"]]["tool_version"]
        or action.get("content_hash") != candidates[action["name"]]["content_hash"]
        for action in actions
    ):
        raise ValueError("本次审批含不可会话放行的工具或强制审批约束")
    if scope == SessionApprovalScope.TOOL and len(names) != 1:
        raise ValueError("本对话允许此工具仅接受同一工具的审批动作")
    selected = list(candidates.values()) if scope == SessionApprovalScope.ALL else [candidates[next(iter(names))]]
    existing = session.approval_policy or {}
    if existing.get("mode") == SessionApprovalMode.ALLOW_ALL and not get_config().MCP_SESSION_ALLOW_ALL:
        existing = {}
    merged = {item["tool_id"]: item for item in existing.get("tools", [])}
    merged.update({item["tool_id"]: item for item in selected})
    mode = (
        SessionApprovalMode.ALLOW_ALL
        if scope == SessionApprovalScope.ALL or existing.get("mode") == SessionApprovalMode.ALLOW_ALL
        else SessionApprovalMode.ALLOW_TOOLS
    )
    return {
        "session_id": session.id,
        "version": session.approval_policy_version,
        "policy": {
            "mode": mode,
            "tools": list(merged.values()),
        },
    }


async def save_session_grant(db: AsyncSession, run: AgentRun, grant: dict) -> None:
    """同审批事务以独立版本写会话策略，并记录运行事件与身份审计。"""
    from app.services.run_manager import RunConflictError

    result = await db.execute(
        update(AgentSession)
        .where(AgentSession.id == grant["session_id"], AgentSession.owner_id == run.owner_id, AgentSession.approval_policy_version == grant["version"])
        .values(approval_policy=grant["policy"], approval_policy_version=AgentSession.approval_policy_version + 1)
        .execution_options(synchronize_session=False),
    )
    if result.rowcount != 1:
        raise RunConflictError("会话放行策略已变化，请刷新后重试")
    await db.execute(update(AgentRun).where(AgentRun.id == run.id).values(event_seq=AgentRun.event_seq + 1).execution_options(synchronize_session=False))
    await db.refresh(run)
    db.add(
        AgentRunEvent(
            run_id=run.id,
            sequence=run.event_seq,
            protocol_version=1,
            event_type=RunEventType.APPROVAL_POLICY_CHANGED,
            state=run.state,
            details={
                "mode": grant["policy"]["mode"],
                "tool_ids": [item["tool_id"] for item in grant["policy"]["tools"]],
                "policy_version": grant["version"] + 1,
            },
        )
    )
    audit(db, "session.approval_granted", actor_id=run.owner_id, target_id=run.agent_id, subject=run.session_id)


def session_policy_output(session: AgentSession) -> dict:
    """输出不含连接参数、凭据或内部摘要的会话授权资料。"""
    policy = session.approval_policy or {}
    return {
        "mode": policy.get("mode", SessionApprovalMode.ASK),
        "version": session.approval_policy_version,
        "tools": [
            {
                "tool_id": item["tool_id"],
                "tool_version": item["tool_version"],
                "name": item["name"],
            }
            for item in policy.get("tools", [])
        ],
        "allow_all_available": get_config().MCP_SESSION_ALLOW_ALL and not get_config().MCP_FORCE_APPROVAL,
        "force_approval": get_config().MCP_FORCE_APPROVAL,
    }


async def revoke_session_grants(db: AsyncSession, session: AgentSession, expected_version: int, tool_id: int | None = None) -> None:
    """按策略版本撤销全部或一个工具，撤销不修改已记录的单次审批结论。"""
    from app.core.custom_exception import CustomException

    remaining = [item for item in (session.approval_policy or {}).get("tools", []) if item["tool_id"] != tool_id] if tool_id else []
    policy = {
        "mode": (session.approval_policy or {}).get("mode", SessionApprovalMode.ALLOW_TOOLS) if remaining else SessionApprovalMode.ASK,
        "tools": remaining,
    }
    result = await db.execute(
        update(AgentSession)
        .where(AgentSession.id == session.id, AgentSession.owner_id == session.owner_id, AgentSession.approval_policy_version == expected_version)
        .values(approval_policy=policy, approval_policy_version=AgentSession.approval_policy_version + 1)
        .execution_options(synchronize_session=False),
    )
    if result.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="会话放行策略已变化，请刷新后重试")
    audit(db, "session.approval_revoked", actor_id=session.owner_id, target_id=session.agent_id, subject=session.id)
    await db.refresh(session)
