# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:12:01
# @Author  : yangyuexiong
# @File    : mcp_connections.py

"""连接配置发布、凭据保护和执行前授权，多个 Tool 复用同一策略。"""

import re
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import McpTransport, RunState, Visibility
from app.models.mcp_connection import McpConnection
from app.models.tool import RunTool, UserTool, UserToolVersion
from app.services.provider_credentials import decrypt_credentials, encrypt_credentials
from app.services.mcp_auth import auth_config, http_credentials


def stdio_profiles() -> dict[str, dict]:
    """检查管理员提供的固定镜像和命令，不允许用户指定镜像或宿主机程序。"""
    profiles = get_config().MCP_STDIO_PROFILES
    for name, profile in profiles.items():
        image = profile.get("image", "")
        command = profile.get("command")
        args = profile.get("args", [])
        keys = profile.get("env_keys", [])
        if not isinstance(image, str) or not re.fullmatch(r"(?:sha256:|[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:)[0-9a-f]{64}", image):
            raise ValueError(f"stdio 配置 {name} 必须固定镜像 ID 或摘要")
        if (
            not isinstance(command, list)
            or not command
            or len(command) > 32
            or any(not isinstance(part, str) or not part or "\0" in part or len(part) > 512 for part in command)
        ):
            raise ValueError(f"stdio 配置 {name} 缺少有效的固定命令")
        if not isinstance(args, list) or len(args) > 32 or any(not isinstance(part, str) or "\0" in part or len(part) > 512 for part in args):
            raise ValueError("stdio 固定参数无效")
        if not isinstance(keys, list) or any(not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) for key in keys):
            raise ValueError("stdio 环境变量白名单无效")
    return profiles


def connection_config(data: dict, previous: McpConnection | None = None) -> dict:
    """校验互斥字段，发布不含凭据的完整传输快照。"""
    from app.services.user_tools import validate_mcp_url

    if data["transport"] == McpTransport.STREAMABLE_HTTP:
        if not data.get("url") or data.get("stdio_profile") or data.get("args") or data.get("env"):
            raise ValueError("HTTP 连接需要 URL，不能填写 stdio 配置、参数或环境变量")
        validate_mcp_url(data["url"])
        if (
            previous
            and previous.config["transport"] == McpTransport.STREAMABLE_HTTP
            and "auth_type" not in previous.config
            and not any(key in data for key in ("auth_type", "api_key", "api_key_header", "headers", "sensitive_headers"))
        ):
            return {
                "transport": McpTransport.STREAMABLE_HTTP,
                "url": data["url"],
            }
        old_config = dict(previous.config) if previous and previous.config["transport"] == McpTransport.STREAMABLE_HTTP else {}
        if old_config and "auth_type" not in old_config:
            old_key, _ = decrypt_credentials(previous.encrypted_credentials)
            old_config["auth_type"] = "bearer" if old_key else "none"
        return {
            "transport": McpTransport.STREAMABLE_HTTP,
            "url": data["url"],
            **auth_config(data, old_config),
        }
    if any(data.get(key) for key in ("url", "bearer_token", "api_key", "auth_type", "api_key_header", "headers", "sensitive_headers")):
        raise ValueError("stdio 连接不能填写 HTTP 地址、鉴权或 Headers")
    profile = stdio_profiles().get(data.get("stdio_profile"))
    if profile is None:
        raise ValueError("stdio 启动配置不存在，请由管理员先配置固定镜像和命令")
    env = data.get("env", {})
    if not set(env) <= set(profile.get("env_keys", [])) or len(env) > 32:
        raise ValueError("stdio 环境变量不在管理员白名单内")
    if any(not isinstance(value, str) or "\n" in value or "\r" in value or "\0" in value or len(value) > 8192 for value in env.values()):
        raise ValueError("stdio 环境变量值无效或过长")
    if any("\0" in arg for arg in data.get("args", [])):
        raise ValueError("stdio 参数包含无效字符")
    return {
        "transport": McpTransport.STDIO,
        "stdio_profile": data["stdio_profile"],
        "image": profile["image"],
        "command": profile["command"],
        "args": [*profile.get("args", []), *data.get("args", [])],
        "user_args": data.get("args", []),
        "env_keys": profile.get("env_keys", []),
    }


def credentials_for(data: dict, previous: McpConnection | None = None) -> str | None:
    """凭据只加密写入；同传输的空值保留，显式清除或替换会触发撤销。"""
    if data["transport"] == McpTransport.STREAMABLE_HTTP:
        config = connection_config(data, previous)
        same_transport = previous and previous.config["transport"] == McpTransport.STREAMABLE_HTTP
        return http_credentials(data, config, previous.encrypted_credentials if same_transport else None, previous.config if same_transport else None)
    if data.get("clear_credentials"):
        if data.get("bearer_token") or data.get("env"):
            raise ValueError("清除凭据与替换凭据不能同时提交")
        return None
    if data.get("bearer_token") or data.get("env"):
        return encrypt_credentials(data.get("bearer_token"), data.get("env", {}))
    if previous and previous.config["transport"] == data["transport"]:
        return previous.encrypted_credentials
    return None


def connection_output(item: McpConnection) -> dict:
    """仅向所有者返回公开连接配置和凭据存在标记。"""
    token, secret_headers = decrypt_credentials(item.encrypted_credentials)
    http = item.config["transport"] == McpTransport.STREAMABLE_HTTP
    return {
        "id": item.id,
        "name": item.name,
        "version": item.version,
        "enabled": item.enabled,
        "approval_mode": item.approval_mode,
        "transport": item.config["transport"],
        "auth_type": item.config.get("auth_type", "bearer" if http and token else "none"),
        "api_key_header": item.config.get("api_key_header"),
        "header_names": sorted((item.config.get("headers") or {}).keys()),
        "sensitive_header_names": sorted(secret_headers) if http else [],
        "has_auth_key": bool(token) if http else False,
        "config_revoked_through": item.config_revoked_through or 0,
        "url": item.config.get("url"),
        "stdio_profile": item.config.get("stdio_profile"),
        "args": item.config.get("user_args", []),
        "has_credentials": bool(item.encrypted_credentials),
        "share_credentials": item.share_credentials,
        "credentials_revoked_through": item.credentials_revoked_through,
    }


async def owned_connection(db: AsyncSession, connection_id: int, owner_id: int, *, lock: bool = False) -> McpConnection:
    """连接管理和发布仅接受自己的连接，跨用户按不存在处理。"""
    query = select(McpConnection).where(McpConnection.id == connection_id, McpConnection.owner_id == owner_id, McpConnection.is_deleted == 0)
    item = await db.scalar(query.with_for_update() if lock else query)
    if item is None:
        raise CustomException(status_code=404, custom_code=10002, detail="MCP 连接不存在")
    return item


async def bind_connection(db: AsyncSession, fields: dict, owner_id: int) -> None:
    """发布时固定共享连接的配置及凭据，后续普通配置更新不改变旧版工具。"""
    item = await owned_connection(db, fields["mcp_connection_id"], owner_id)
    if not item.enabled:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接已停用")
    if fields.get("visibility") == Visibility.PUBLIC and item.encrypted_credentials and not item.share_credentials:
        raise CustomException(status_code=409, custom_code=10005, detail="公开工具前须允许共享连接凭据")
    fields.update(
        {
            "mcp_connection_version": item.version,
            "connection_approval_mode": item.approval_mode,
            "mcp_config": dict(item.config),
            "mcp_url": item.config.get("url"),
            "encrypted_credentials": item.encrypted_credentials,
            "share_credentials": item.share_credentials,
        }
    )


async def require_connection(db: AsyncSession, tool: UserTool, version: UserToolVersion, actor_id: int) -> None:
    """旧版执行快照仍受当前连接停用、删除、密钥撤销和共享策略限制。"""
    if version.mcp_connection_id is None:
        return
    from app.services.mcp_access import can_borrow_credentials

    item = await db.get(McpConnection, version.mcp_connection_id)
    if (
        item is None
        or item.is_deleted != 0
        or not item.enabled
        or item.owner_id != tool.owner_id
        or (version.mcp_connection_version or 0) <= (item.config_revoked_through or 0)
        or version.encrypted_credentials
        and (version.mcp_connection_version or 0) <= item.credentials_revoked_through
        or not await can_borrow_credentials(db, tool, version, actor_id)
    ):
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接授权或凭据版本已失效")


async def cancel_connection_runs(db: AsyncSession, item: McpConnection, *, others_only: bool = False) -> None:
    """连接策略收紧时取消所有相关活动运行，包括等待审批的运行。"""
    from app.models.agent_run import AgentRun
    from app.services.run_manager import request_cancel

    matching_runs = (
        select(RunTool.run_id)
        .join(
            UserToolVersion,
            (UserToolVersion.tool_id == RunTool.tool_id) & (UserToolVersion.version == RunTool.tool_version),
        )
        .where(UserToolVersion.mcp_connection_id == item.id)
    )
    query = select(AgentRun).where(AgentRun.id.in_(matching_runs))
    if others_only:
        query = query.where(AgentRun.owner_id != item.owner_id)
    for run in (await db.scalars(query)).all():
        if run.state not in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT):
            await request_cancel(db, run.id, run.owner_id)
