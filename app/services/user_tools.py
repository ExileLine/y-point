# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : user_tools.py

"""用户 Tool 版本校验、可见性及运行快照。"""

import ast
from collections import Counter
import hashlib
import json
import re
import socket
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RecoveryPolicy, RunState, ScriptLanguage, ToolApprovalMode, ToolCallability, ToolEffect, ToolKind, Visibility
from app.core.custom_exception import CustomException
from app.models.tool import AgentTool, RunTool, UserTool, UserToolVersion
from app.services.provider_credentials import encrypt_credentials
from app.services.tool_environments import normalize_requirements

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_SOURCE_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 16 * 1024


def tool_display_name(tool: UserTool) -> str:
    """返回可读显示名称，兼容既有工具和 Skill 管理的脚本。"""
    return tool.display_name or tool.name


def model_tool_name(tool: UserTool) -> str:
    """生成跨所有者唯一、不会覆盖内置工具的模型可见名称。"""
    return f"user_tool_{tool.id}_{tool.name}"


def tool_name_aliases(tools: list[UserTool], reserved_names: set[str] | frozenset[str]) -> dict[str, str]:
    """仅为本轮唯一且不与注册名称冲突的工具生成自然名称别名。"""
    counts = Counter(tool.name for tool in tools)
    reserved = set(reserved_names) | {model_tool_name(tool) for tool in tools}
    return {tool.name: model_tool_name(tool) for tool in tools if counts[tool.name] == 1 and tool.name not in reserved}


def skill_tool_catalog(tools: list[UserTool], aliases: dict[str, str]) -> str:
    """生成本轮已授权工具名称映射，不将 Skill 描述视为新增授权。"""
    lines = [
        "\n\n## 本轮平台工具映射（由服务端生成）",
        "Skill 中的工具名称或同名 .py/.sh 脚本指向下列已注册 Tool；调用对应模型工具，不要尝试读取或执行不存在的脚本文件。",
        "Skill 的 allowed-tools 和文字描述不会新增工具权限。未列出的工具不可调用；重名时须根据明确的工具 ID/所有者选择，否则请用户澄清。",
    ]
    for tool in tools:
        status = "名称唯一" if tool.name in aliases else "名称冲突，必须明确工具 ID/所有者"
        lines.append(f"- {tool_display_name(tool)}（内部标识={tool.name}，Tool ID={tool.id}，所有者={tool.owner_id}，{status}）→ {model_tool_name(tool)}")
    if not tools:
        lines.append("- 本轮未关联可用的用户 Tool。")
    return "\n".join(lines)


def validate_mcp_url(url: str) -> str:
    """允许 HTTP/HTTPS MCP 端点，按实际端口解析并返回固定连接 IP。"""
    try:
        parts = urlsplit(url)
        port = parts.port
        valid = (
            parts.scheme in ("http", "https")
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and (port is None or 1 <= port <= 65535)
            and not any(char.isspace() or ord(char) < 32 for char in url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("MCP 地址必须是无内嵌凭据的 HTTP 或 HTTPS 地址，端口范围为 1–65535")
    try:
        addresses = socket.getaddrinfo(parts.hostname, port or (80 if parts.scheme == "http" else 443), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("MCP 域名无法解析") from exc
    if not addresses:
        raise ValueError("MCP 域名无法解析")
    return addresses[0][4][0]


def validate_tool_definition(kind: str, name: str, fields: dict) -> tuple[str, str, bool, str]:
    """校验脚本或 MCP 执行定义，并返回读写属性、审批策略及摘要。"""
    if TOOL_NAME_RE.fullmatch(name) is None:
        raise ValueError("Tool 内部调用标识只能使用小写字母、数字和下划线，且以字母开头")
    schema = fields.get("input_schema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("Tool 参数结构必须是对象型 JSON Schema（type: object），不能提交示例参数或返回结果")
    if len(json.dumps(schema).encode()) > MAX_SCHEMA_BYTES:
        raise ValueError("Tool 参数结构不能超过 16 KiB")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Tool 参数 JSON Schema 无效") from exc
    requirements = normalize_requirements(fields.get("requirements"))
    if requirements and (kind != ToolKind.SCRIPT or fields.get("script_language") != ScriptLanguage.PYTHON):
        raise ValueError("只有 Python 脚本 Tool 可以声明 pip 依赖")
    if kind == ToolKind.SCRIPT:
        language, source = fields.get("script_language"), fields.get("source_code")
        if language not in ScriptLanguage or not isinstance(source, str) or not 1 <= len(source.encode()) <= MAX_SOURCE_BYTES:
            raise ValueError("脚本需提供 64 KiB 内的 Python 或 Bash 源码")
        if any(fields.get(key) for key in ("mcp_url", "mcp_connection_id", "mcp_tool_name", "mcp_bearer_token", "encrypted_credentials")):
            raise ValueError("脚本 Tool 不能包含 MCP 连接配置")
        if language == ScriptLanguage.PYTHON:
            try:
                ast.parse(source)
            except SyntaxError as exc:
                raise ValueError("Python 脚本语法无效") from exc
        effect, approval = ToolEffect.READ, fields.get("requires_approval", False)
        fields["recovery_policy"] = RecoveryPolicy.SAFE
        fields["idempotency_key_field"] = None
    elif kind == ToolKind.MCP:
        url, remote_name = fields.get("mcp_url"), fields.get("mcp_tool_name")
        if (not fields.get("mcp_config") and not isinstance(url, str)) or not isinstance(remote_name, str) or not remote_name or len(remote_name) > 128:
            raise ValueError("MCP Tool 需要连接地址与远端工具名")
        if not fields.get("mcp_config"):
            validate_mcp_url(url)
        if fields.get("source_code") or fields.get("script_language"):
            raise ValueError("MCP Tool 不能包含脚本源码")
        if fields.get("visibility") == Visibility.PUBLIC and fields.get("encrypted_credentials") and not fields.get("share_credentials"):
            raise ValueError("公开 MCP Tool 需明确授权共享连接凭据")
        effect = fields.get("effect") or ToolEffect.WRITE
        mode = fields.get("approval_mode")
        if mode in (None, ToolApprovalMode.INHERIT):
            mode = fields.get("connection_approval_mode", ToolApprovalMode.WRITES)
            if fields.get("approval_mode") is None and fields.get("requires_approval"):
                mode = ToolApprovalMode.ALWAYS
        approval = mode == ToolApprovalMode.ALWAYS or mode == ToolApprovalMode.WRITES and effect != ToolEffect.READ
        policy = fields.get("recovery_policy") or (RecoveryPolicy.SAFE if effect == ToolEffect.READ else RecoveryPolicy.MANUAL)
        key = fields.get("idempotency_key_field")
        if effect == ToolEffect.READ and policy != RecoveryPolicy.SAFE:
            raise ValueError("只读 MCP 使用 safe 恢复策略")
        if effect == ToolEffect.WRITE and policy not in (RecoveryPolicy.MANUAL, RecoveryPolicy.IDEMPOTENT):
            raise ValueError("外部写 MCP 仅支持人工核对或已声明幂等键的策略")
        if policy == RecoveryPolicy.IDEMPOTENT and (not key or schema.get("properties", {}).get(key, {}).get("type") != "string"):
            raise ValueError("幂等写工具必须声明顶层字符串幂等键参数")
        fields["recovery_policy"] = policy
        if policy != RecoveryPolicy.IDEMPOTENT:
            fields["idempotency_key_field"] = None
    else:
        raise ValueError("Tool 类型不受支持")
    fields["requires_approval"] = bool(approval)
    fields["effect"] = effect
    fields.pop("effect_explicit", None)
    fields.pop("read_only_reason", None)
    fields.pop("connection_approval_mode", None)
    digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return effect, kind, approval, digest


def version_fields(body: dict, previous: UserToolVersion | None = None) -> dict:
    """构造新版本的完整执行定义并加密新增凭据。"""
    keys = (
        "requirements",
        "input_schema",
        "script_language",
        "source_code",
        "mcp_url",
        "mcp_connection_id",
        "mcp_connection_version",
        "mcp_config",
        "mcp_tool_name",
        "share_credentials",
        "encrypted_credentials",
        "recovery_policy",
        "idempotency_key_field",
        "requires_approval",
        "approval_mode",
        "effect_source",
        "mcp_annotations",
        "read_confirmation",
    )
    fields = {key: getattr(previous, key) if previous is not None else None for key in keys}
    fields.update({key: value for key, value in body.items() if key in keys})
    if "approval_mode" in body and "requires_approval" in body:
        raise ValueError("审批覆盖与兼容审批开关不能同时提交")
    if "approval_mode" in body:
        fields["approval_mode"] = body["approval_mode"]
    elif "requires_approval" in body:
        fields["approval_mode"] = ToolApprovalMode.ALWAYS if body["requires_approval"] else ToolApprovalMode.WRITES
    elif fields["approval_mode"] is None:
        fields["approval_mode"] = ToolApprovalMode.ALWAYS if fields["requires_approval"] else ToolApprovalMode.INHERIT
    if previous is None and "requires_approval" not in body:
        fields["requires_approval"] = False
    fields["read_only_reason"] = body.get("read_only_reason")
    fields["effect_explicit"] = "effect" in body
    fields["effect"] = body.get("effect", previous.effect if previous is not None else None)
    if body.get("clear_mcp_credentials"):
        if body.get("mcp_bearer_token"):
            raise ValueError("清除凭据与替换凭据不能同时提交")
        fields["encrypted_credentials"] = None
        fields["share_credentials"] = False
    fields["requirements"] = normalize_requirements(fields.get("requirements"))
    token = body.get("mcp_bearer_token")
    if token:
        fields["encrypted_credentials"] = encrypt_credentials(token, {})
    fields["share_credentials"] = bool(fields["share_credentials"])
    if body.get("mcp_connection_id") is not None and any(body.get(key) for key in ("mcp_url", "mcp_bearer_token", "clear_mcp_credentials")):
        raise ValueError("选择共享连接后，请在连接管理中修改地址和凭据")
    if body.get("mcp_url") or body.get("mcp_bearer_token") or body.get("clear_mcp_credentials"):
        if previous is not None and previous.mcp_connection_id is not None:
            raise ValueError("此工具使用共享连接，请在连接管理中修改地址和凭据")
    fields["visibility"] = body["visibility"]
    return fields


async def selected_tools(db: AsyncSession, tool_ids: list[int], actor_id: int) -> list[UserTool]:
    """确认 Agent 可直接关联的工具，明确共享与公开授权均按实际使用者检查。"""
    from app.services.mcp_access import can_access_tool, can_borrow_credentials

    if len(tool_ids) != len(set(tool_ids)):
        raise CustomException(status_code=400, custom_code=10005, detail="关联的 Tool 不能重复")
    items = list(await db.scalars(select(UserTool).where(UserTool.id.in_(tool_ids)).with_for_update())) if tool_ids else []
    if len(items) != len(tool_ids):
        raise CustomException(status_code=404, custom_code=10002, detail="Tool 不存在或不可用")
    for item in items:
        version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == item.id, UserToolVersion.version == item.current_version))
        if (
            item.is_deleted != 0
            or item.skill_id is not None
            or not item.enabled
            or version is None
            or not version.verified
            or not await can_access_tool(db, item, version, actor_id)
        ):
            raise CustomException(status_code=404, custom_code=10002, detail="Tool 不存在或不可用")
        if not await can_borrow_credentials(db, item, version, actor_id):
            raise CustomException(status_code=409, custom_code=10005, detail=f"工具 #{item.id} 未获凭据借用权限，请所有者授权后再关联")
    return items


async def agent_tool_rows(db: AsyncSession, agent_id: int, *, direct_only: bool = False) -> list:
    """展开直接关联与连接选择；相同版本去重，不同版本明确拒绝。"""
    rows = list(await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent_id).order_by(AgentTool.id)))
    if direct_only:
        return rows
    from app.services.agent_mcp import connection_bindings, validate_binding_versions

    grouped = await connection_bindings(db, agent_id)
    validate_binding_versions({row.tool_id: row.tool_version for row in rows}, grouped)
    merged = {row.tool_id: row for row in rows}
    for row in grouped:
        merged.setdefault(row.tool_id, row)
    return list(merged.values())


async def snapshot_run_tools(db: AsyncSession, run_id: str, agent_id: int, actor_id: int) -> None:
    """固定明确选择的工具版本；连接选择缺权时返回修复提示，不静默丢弃。"""
    rows = await agent_tool_rows(db, agent_id)
    from app.services.mcp_access import can_access_tool, require_group_access
    from app.services.agent_mcp import connection_bindings

    grouped_rows = await connection_bindings(db, agent_id)
    grouped_ids = {row.tool_id for row in grouped_rows}
    for grouped in grouped_rows:
        await require_group_access(db, grouped.connection_id, grouped.tool_id, actor_id)
    if not rows:
        return
    items = (
        await db.scalars(
            select(UserTool).where(
                UserTool.id.in_([row.tool_id for row in rows]),
                UserTool.is_deleted == 0,
                UserTool.skill_id.is_(None),
                UserTool.enabled.is_(True),
            ),
        )
    ).all()
    by_id = {item.id: item for item in items}
    for row in rows:
        if row.tool_id not in by_id:
            if row.tool_id in grouped_ids:
                raise CustomException(status_code=409, custom_code=10005, detail=f"连接工具 #{row.tool_id} 已停用或移除，请修复关联后重试")
            continue
        version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == row.tool_id, UserToolVersion.version == row.tool_version))
        if version is None or not version.verified or not await can_access_tool(db, by_id[row.tool_id], version, actor_id):
            if row.tool_id in grouped_ids:
                raise CustomException(status_code=409, custom_code=10005, detail=f"连接工具 #{row.tool_id} 的授权或固定版本已失效，请修复关联后重试")
            continue
        current_number = by_id[row.tool_id].current_version
        policy = (
            version
            if version.version == current_number
            else await db.scalar(
                select(UserToolVersion).where(UserToolVersion.tool_id == row.tool_id, UserToolVersion.version == current_number),
            )
        )
        if policy is None:
            raise CustomException(status_code=409, custom_code=10005, detail="Tool 当前审批配置不可用")
        from app.models.agent_run import AgentRun
        from app.services.mcp_policy import tool_callability

        run = await db.get(AgentRun, run_id)
        decision = await tool_callability(db, by_id[row.tool_id], version, actor_id, policy_version=policy, require_all=run.require_mcp_approval)
        if decision["status"] == ToolCallability.BLOCKED:
            raise CustomException(status_code=409, custom_code=10005, detail=decision["message"])
        db.add(
            RunTool(
                run_id=run_id,
                tool_id=row.tool_id,
                tool_version=row.tool_version,
                mcp_connection_id=getattr(row, "connection_id", None),
                content_hash=version.content_hash,
                requires_approval=decision["policy"]["requires_approval"],
                approval_policy=decision["policy"],
                is_deleted=0,
                status=1,
            ),
        )


async def load_run_tools(db: AsyncSession, run_id: str, actor_id: int) -> list[tuple[UserTool, UserToolVersion]]:
    """在执行前重新核对版本摘要、状态与当前用户权限。"""
    rows = (await db.scalars(select(RunTool).where(RunTool.run_id == run_id).order_by(RunTool.id))).all()
    result = []
    from app.services.mcp_access import can_access_tool, can_borrow_credentials, require_group_access

    for row in rows:
        if row.mcp_connection_id is not None:
            await require_group_access(db, row.mcp_connection_id, row.tool_id, actor_id)
        tool = await db.get(UserTool, row.tool_id)
        version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == row.tool_id, UserToolVersion.version == row.tool_version))
        if (
            tool is None
            or tool.is_deleted != 0
            or not tool.enabled
            or not await can_access_tool(db, tool, version, actor_id)
            or version is None
            or not version.verified
            or version.encrypted_credentials is not None
            and version.version <= (tool.credentials_revoked_through or 0)
            or not await can_borrow_credentials(db, tool, version, actor_id)
            or version.content_hash != row.content_hash
        ):
            raise CustomException(status_code=409, custom_code=10005, detail="Tool 授权或版本已失效")
        from app.models.agent_run import AgentRun
        from app.services.mcp_policy import tool_callability

        run = await db.get(AgentRun, run_id)
        decision = await tool_callability(db, tool, version, actor_id, require_all=bool(run and run.require_mcp_approval))
        if decision["status"] == ToolCallability.BLOCKED:
            raise CustomException(status_code=409, custom_code=10005, detail=decision["message"])
        if tool.kind == ToolKind.MCP and row.requires_approval is False and decision["policy"]["requires_approval"]:
            raise CustomException(status_code=409, custom_code=10005, detail="Tool 审批策略已收紧，请重新提交运行")
        if tool.skill_id is not None:
            from app.services.skill_package_runtime import managed_script_resources

            await managed_script_resources(db, run_id, actor_id, tool, version, read_files=False)
        result.append((tool, version))
    return result


async def _credentials_shared(db: AsyncSession, tool: UserTool) -> bool:
    """凭据共享以当前发布规则为准，历史快照不能绕过撤销。"""
    current = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version))
    return current is not None and current.share_credentials


async def cancel_tool_runs(db: AsyncSession, tool: UserTool, *, credentials_only: bool = False, others_only: bool = False) -> int:
    """取消受禁用、撤权或密钥轮换影响的运行，等待审批的运行同步关闭。"""
    from app.models.agent_run import AgentRun
    from app.services.run_manager import request_cancel

    query = select(AgentRun).join(RunTool, RunTool.run_id == AgentRun.id).where(RunTool.tool_id == tool.id)
    if credentials_only:
        query = query.join(
            UserToolVersion,
            (UserToolVersion.tool_id == RunTool.tool_id) & (UserToolVersion.version == RunTool.tool_version),
        ).where(UserToolVersion.encrypted_credentials.is_not(None), RunTool.tool_version <= tool.credentials_revoked_through)
    if others_only:
        query = query.where(AgentRun.owner_id != tool.owner_id)
    count = 0
    for run in (await db.scalars(query)).all():
        if run.state not in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT):
            await request_cancel(db, run.id, run.owner_id)
            count += 1
    return count
