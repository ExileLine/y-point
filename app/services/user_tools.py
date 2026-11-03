# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : user_tools.py

"""用户 Tool 版本校验、可见性及运行快照。"""

import ast
from collections import Counter
import hashlib
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RecoveryPolicy, RunState, ScriptLanguage, ToolEffect, ToolKind, Visibility
from app.core.custom_exception import CustomException
from app.models.tool import AgentTool, RunTool, UserTool, UserToolVersion
from app.services.provider_credentials import encrypt_credentials
from app.services.tool_environments import normalize_requirements

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_SOURCE_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 16 * 1024
MAX_AGENT_TOOLS = 16


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
        lines.append(f"- {tool.name}（Tool ID={tool.id}，所有者={tool.owner_id}，{status}）→ {model_tool_name(tool)}")
    if not tools:
        lines.append("- 本轮未关联可用的用户 Tool。")
    return "\n".join(lines)


def validate_mcp_url(url: str) -> str:
    """仅允许公网 HTTPS MCP 端点，并返回本次固定的连接 IP。"""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment or parts.port not in (None, 443):
        raise ValueError("MCP 地址必须是无凭据的公网 HTTPS 地址")
    try:
        addresses = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("MCP 域名无法解析") from exc
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("MCP 地址不得指向内网、回环或保留地址")
    return addresses[0][4][0]


def validate_tool_definition(kind: str, name: str, fields: dict) -> tuple[str, str, bool, str]:
    """校验脚本或 MCP 执行定义，并返回固定策略及摘要。"""
    if TOOL_NAME_RE.fullmatch(name) is None:
        raise ValueError("Tool 名称只能使用小写字母、数字和下划线，且以字母开头")
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
        if any(fields.get(key) for key in ("mcp_url", "mcp_tool_name", "mcp_bearer_token", "encrypted_credentials")):
            raise ValueError("脚本 Tool 不能包含 MCP 连接配置")
        if language == ScriptLanguage.PYTHON:
            try:
                ast.parse(source)
            except SyntaxError as exc:
                raise ValueError("Python 脚本语法无效") from exc
        effect, approval = ToolEffect.READ, False
        fields["recovery_policy"] = RecoveryPolicy.SAFE
        fields["idempotency_key_field"] = None
    elif kind == ToolKind.MCP:
        url, remote_name = fields.get("mcp_url"), fields.get("mcp_tool_name")
        if not isinstance(url, str) or not isinstance(remote_name, str) or not remote_name or len(remote_name) > 128:
            raise ValueError("MCP Tool 需要连接地址与远端工具名")
        validate_mcp_url(url)
        if fields.get("source_code") or fields.get("script_language"):
            raise ValueError("MCP Tool 不能包含脚本源码")
        if fields.get("visibility") == Visibility.PUBLIC and fields.get("encrypted_credentials") and not fields.get("share_credentials"):
            raise ValueError("公开 MCP Tool 需明确授权共享连接凭据")
        effect, approval = fields.get("effect") or ToolEffect.WRITE, True
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
        "mcp_tool_name",
        "share_credentials",
        "encrypted_credentials",
        "recovery_policy",
        "idempotency_key_field",
    )
    fields = {key: getattr(previous, key) if previous is not None else None for key in keys}
    fields.update({key: value for key, value in body.items() if key in keys})
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
    fields["visibility"] = body["visibility"]
    return fields


async def selected_tools(db: AsyncSession, tool_ids: list[int], actor_id: int) -> list[UserTool]:
    """确认 Agent 可关联的私有或公开 Tool。"""
    if len(tool_ids) != len(set(tool_ids)) or len(tool_ids) > MAX_AGENT_TOOLS:
        raise CustomException(status_code=400, custom_code=10005, detail="最多关联 16 个不重复的 Tool")
    items = (await db.scalars(select(UserTool).where(UserTool.id.in_(tool_ids)).with_for_update())).all() if tool_ids else []
    verified_versions = (
        {
            (item.tool_id, item.version)
            for item in (
                await db.scalars(
                    select(UserToolVersion).where(
                        UserToolVersion.tool_id.in_(tool_ids),
                        UserToolVersion.verified.is_(True),
                    )
                )
            ).all()
        }
        if tool_ids
        else {}
    )
    if len(items) != len(tool_ids) or any(
        item.is_deleted != 0
        or not item.enabled
        or (item.id, item.current_version) not in verified_versions
        or (item.owner_id != actor_id and item.visibility != Visibility.PUBLIC)
        for item in items
    ):
        raise CustomException(status_code=404, custom_code=10002, detail="Tool 不存在或不可用")
    return items


async def agent_tool_rows(db: AsyncSession, agent_id: int) -> list[AgentTool]:
    """读取 Agent 直接关联的 Tool 版本。"""
    return (await db.scalars(select(AgentTool).where(AgentTool.agent_id == agent_id).order_by(AgentTool.id))).all()


async def snapshot_run_tools(db: AsyncSession, run_id: str, agent_id: int, actor_id: int) -> None:
    """为本轮固定当前用户有权使用的 Agent Tool；共享 Agent 的私有 Tool 自动排除。"""
    rows = await agent_tool_rows(db, agent_id)
    if not rows:
        return
    items = (
        await db.scalars(
            select(UserTool).where(
                UserTool.id.in_([row.tool_id for row in rows]),
                UserTool.is_deleted == 0,
                UserTool.enabled.is_(True),
                or_(UserTool.owner_id == actor_id, UserTool.visibility == Visibility.PUBLIC),
            )
        )
    ).all()
    by_id = {item.id: item for item in items}
    for row in rows:
        if row.tool_id not in by_id:
            continue
        version = await db.scalar(
            select(UserToolVersion).where(UserToolVersion.tool_id == row.tool_id, UserToolVersion.version == row.tool_version)
        )
        if version is None or not version.verified:
            continue
        db.add(
            RunTool(
                run_id=run_id,
                tool_id=row.tool_id,
                tool_version=row.tool_version,
                content_hash=version.content_hash,
                is_deleted=0,
                status=1,
            )
        )


async def load_run_tools(db: AsyncSession, run_id: str, actor_id: int) -> list[tuple[UserTool, UserToolVersion]]:
    """在执行前重新核对版本摘要、状态与当前用户权限。"""
    rows = (await db.scalars(select(RunTool).where(RunTool.run_id == run_id).order_by(RunTool.id))).all()
    result = []
    for row in rows:
        tool = await db.get(UserTool, row.tool_id)
        version = await db.scalar(
            select(UserToolVersion).where(UserToolVersion.tool_id == row.tool_id, UserToolVersion.version == row.tool_version)
        )
        if (
            tool is None
            or tool.is_deleted != 0
            or not tool.enabled
            or tool.owner_id != actor_id
            and tool.visibility != Visibility.PUBLIC
            or version is None
            or not version.verified
            or version.encrypted_credentials is not None
            and version.version <= (tool.credentials_revoked_through or 0)
            or tool.owner_id != actor_id
            and version.encrypted_credentials is not None
            and not await _credentials_shared(db, tool)
            or version.content_hash != row.content_hash
        ):
            raise CustomException(status_code=409, custom_code=10005, detail="Tool 授权或版本已失效")
        result.append((tool, version))
    return result


async def _credentials_shared(db: AsyncSession, tool: UserTool) -> bool:
    """凭据共享以当前发布规则为准，历史快照不能绕过撤销。"""
    current = await db.scalar(
        select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version)
    )
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
