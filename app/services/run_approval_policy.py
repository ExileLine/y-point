# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : run_approval_policy.py

"""集中绑定审批动作、脱敏预览与本轮拒绝范围。"""

import hashlib
import hmac
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.enums import ApprovalState, ToolKind
from app.models.run_approval import RunApproval
from app.models.tool import RunTool, UserTool, UserToolVersion
from app.services.user_tools import model_tool_name


def approval_preview(arguments: dict) -> dict:
    """仅展示目标 ID、数量和无凭据的 HTTP 主机；不公开自由文本、路径或查询串。"""
    preview = {}
    for key, value in arguments.items():
        if key in {"count", "limit", "quantity", "offset", "page"} and type(value) is int and 0 <= value <= 1_000_000:
            preview[key] = value
        elif key in {"id", "order_id", "project_id", "record_id", "ticket_id", "artifact_id"} and isinstance(value, (str, int)):
            text = str(value)
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", text):
                preview[key] = text
        elif key in {"ids", "order_ids", "record_ids"} and isinstance(value, list):
            preview[f"{key}_count"] = len(value)
        elif key == "url" and isinstance(value, str):
            try:
                parts = urlsplit(value)
                if parts.scheme in {"http", "https"} and parts.hostname:
                    preview["target_host"] = parts.hostname[:128]
            except ValueError:
                pass
    return preview


async def bind_actions(db: AsyncSession, run_id: str, checkpoint_id: str, actions: list[dict]) -> list[dict]:
    """在参数 HMAC 之外绑定本轮固定 Tool 版本和远端身份。"""
    rows = (
        await db.execute(
            select(UserTool, UserToolVersion)
            .join(RunTool, RunTool.tool_id == UserTool.id)
            .join(UserToolVersion, (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == RunTool.tool_version))
            .where(RunTool.run_id == run_id)
        )
    ).all()
    tools = {model_tool_name(tool): (tool, version) for tool, version in rows}
    bound = []
    for index, action in enumerate(actions):
        item = dict(action)
        match = tools.get(action["name"])
        if match:
            tool, version = match
            item["tool_id"] = tool.id
            item["tool_version"] = version.version
            item["content_hash"] = version.content_hash
            if tool.kind == ToolKind.MCP:
                item["connection_id"] = version.mcp_connection_id
                item["remote_name"] = version.mcp_tool_name
                item["remote_scope"] = hashlib.sha256(json.dumps(version.mcp_config or version.mcp_url, sort_keys=True).encode()).hexdigest()
        identity = json.dumps(
            [run_id, checkpoint_id, index, {key: value for key, value in item.items() if key not in {"preview", "action_id"}}], sort_keys=True
        )
        item["action_id"] = hmac.new(get_config().SECRET_KEY.encode(), identity.encode(), hashlib.sha256).hexdigest()
        bound.append(item)
    return bound


async def _denial_context(db: AsyncSession, run_id: str):
    """一次读取拒绝审计和固定工具版本，避免模型调用前按工具重复查询。"""
    approvals = (await db.scalars(select(RunApproval).where(RunApproval.run_id == run_id, RunApproval.state == ApprovalState.RESOLVED))).all()
    denied = [action for approval in approvals for action, decision in zip(approval.actions, approval.decisions or [], strict=True) if decision == "reject"]
    versions = {}
    if denied:
        rows = (
            await db.execute(
                select(UserTool, UserToolVersion)
                .join(RunTool, RunTool.tool_id == UserTool.id)
                .join(UserToolVersion, (UserToolVersion.tool_id == UserTool.id) & (UserToolVersion.version == RunTool.tool_version))
                .where(RunTool.run_id == run_id)
            )
        ).all()
        versions = {model_tool_name(tool): (tool, version) for tool, version in rows}
    return approvals, denied, versions


def _is_denied(name: str, denied: list[dict], versions: dict) -> bool:
    """拒绝后禁用同名、等价远端操作及所有后续用户写工具。"""
    if any(action["name"] == name for action in denied):
        return True
    candidate = versions.get(name)
    if not denied or candidate is None:
        return False
    tool, version = candidate
    if version.effect == "write":
        return True
    scope = hashlib.sha256(json.dumps(version.mcp_config or version.mcp_url, sort_keys=True).encode()).hexdigest()
    return tool.kind == ToolKind.MCP and any(
        action.get("remote_name") == version.mcp_tool_name
        and (action.get("remote_scope") == scope or action.get("connection_id") is not None and action["connection_id"] == version.mcp_connection_id)
        for action in denied
    )


async def denied_tool_names(db: AsyncSession, run_id: str, names) -> set[str]:
    """批量得出不再向模型暴露或触发新审批的工具集合。"""
    _, denied, versions = await _denial_context(db, run_id)
    return {name for name in names if _is_denied(name, denied, versions)}


async def denied_in_run(db: AsyncSession, run_id: str, name: str, call_id: str | None = None, arguments: dict | None = None) -> bool:
    """执行前重查拒绝记录；本批精确批准的原调用除外，不能换 ID 或参数。"""
    approvals, denied, versions = await _denial_context(db, run_id)
    if call_id is not None and arguments is not None:
        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hmac.new(get_config().SECRET_KEY.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if any(
            action.get("call_id") == call_id and action["name"] == name and action["arguments_hash"] == digest and decision == "approve"
            for approval in approvals
            for action, decision in zip(approval.actions, approval.decisions or [], strict=True)
        ):
            return False
    return _is_denied(name, denied, versions)
