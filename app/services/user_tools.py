# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : user_tools.py

"""用户 Tool 版本校验、可见性及运行快照。"""

import ast
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

from app.core.custom_exception import CustomException
from app.models.tool import AgentTool, RunTool, UserTool, UserToolVersion
from app.services.provider_credentials import encrypt_credentials

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_SOURCE_BYTES = 64 * 1024
MAX_SCHEMA_BYTES = 16 * 1024
MAX_AGENT_TOOLS = 16


def model_tool_name(tool: UserTool) -> str:
    """生成跨所有者唯一、不会覆盖内置工具的模型可见名称。"""
    return f"user_tool_{tool.id}_{tool.name}"


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
    if kind == "script":
        language, source = fields.get("script_language"), fields.get("source_code")
        if language not in {"python", "bash"} or not isinstance(source, str) or not 1 <= len(source.encode()) <= MAX_SOURCE_BYTES:
            raise ValueError("脚本需提供 64 KiB 内的 Python 或 Bash 源码")
        if any(fields.get(key) for key in ("mcp_url", "mcp_tool_name", "mcp_bearer_token", "encrypted_credentials")):
            raise ValueError("脚本 Tool 不能包含 MCP 连接配置")
        if language == "python":
            try:
                ast.parse(source)
            except SyntaxError as exc:
                raise ValueError("Python 脚本语法无效") from exc
        effect, approval = "read", False
    elif kind == "mcp":
        url, remote_name = fields.get("mcp_url"), fields.get("mcp_tool_name")
        if not isinstance(url, str) or not isinstance(remote_name, str) or not remote_name or len(remote_name) > 128:
            raise ValueError("MCP Tool 需要连接地址与远端工具名")
        validate_mcp_url(url)
        if fields.get("source_code") or fields.get("script_language"):
            raise ValueError("MCP Tool 不能包含脚本源码")
        if fields.get("visibility") == "public" and fields.get("encrypted_credentials") and not fields.get("share_credentials"):
            raise ValueError("公开 MCP Tool 需明确授权共享连接凭据")
        effect, approval = "write", True
    else:
        raise ValueError("Tool 类型不受支持")
    digest = hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    return effect, kind, approval, digest


def version_fields(body: dict, previous: UserToolVersion | None = None) -> dict:
    """构造新版本的完整执行定义并加密新增凭据。"""
    keys = ("input_schema", "script_language", "source_code", "mcp_url", "mcp_tool_name", "share_credentials", "encrypted_credentials")
    fields = {key: getattr(previous, key) if previous is not None else None for key in keys}
    fields.update({key: value for key, value in body.items() if key in keys})
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
        or (item.owner_id != actor_id and item.visibility != "public")
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
                or_(UserTool.owner_id == actor_id, UserTool.visibility == "public"),
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
            and tool.visibility != "public"
            or version is None
            or not version.verified
            or version.content_hash != row.content_hash
        ):
            raise CustomException(status_code=409, custom_code=10005, detail="Tool 授权或版本已失效")
        result.append((tool, version))
    return result
