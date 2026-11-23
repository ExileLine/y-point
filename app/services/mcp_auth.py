# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 15:00:00
# @Author  : yangyuexiong
# @File    : mcp_auth.py

"""MCP HTTP 专用鉴权与 Header 校验，发现和执行共用同一组装入口。"""

import re

from app.core.enums import McpAuthType
from app.services.provider_credentials import decrypt_credentials, encrypt_credentials

_RESERVED = {
    "authorization",
    "proxy-authorization",
    "host",
    "connection",
    "content-length",
    "content-type",
    "accept",
    "accept-encoding",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "keep-alive",
    "cookie",
    "set-cookie",
    "origin",
    "referer",
    "user-agent",
}


def header_name(value: str) -> str:
    """拒绝协议控制字段和可造成同源/代理歧义的请求头。"""
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}", value):
        raise ValueError("MCP Header 名称无效")
    name = value.lower()
    if name in _RESERVED or name.startswith(("mcp-", "sec-", "proxy-", "x-forwarded-", "forwarded")):
        raise ValueError("MCP Header 名称属于保留字段")
    return name


def normalize_headers(values: dict, *, removal: bool = False) -> dict:
    """名称不区分大小写，每项最多 4096 字节，空值删除仅用于敏感字段更新。"""
    normalized = {}
    if len(values) > 16:
        raise ValueError("MCP 自定义 Header 最多 16 项")
    for key, value in values.items():
        name = header_name(key)
        if name in normalized:
            raise ValueError("MCP Header 名称不允许大小写重复")
        if value is None and removal:
            normalized[name] = None
        elif not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) > 126 for c in value) or len(value.encode()) > 4096:
            raise ValueError("MCP Header 值须为非空可打印 ASCII，且不超过 4096 字节")
        else:
            normalized[name] = value
    return normalized


def auth_config(data: dict, previous: dict | None = None) -> dict:
    """省略新字段时保留已有配置，旧 Bearer 请求自动使用原语义。"""
    previous = previous or {}
    mode = data.get("auth_type") or (
        McpAuthType.BEARER if data.get("bearer_token") else McpAuthType.API_KEY if data.get("api_key") else previous.get("auth_type", McpAuthType.NONE)
    )
    if mode not in McpAuthType:
        raise ValueError("MCP 鉴权方式无效")
    key_name = data.get("api_key_header") or previous.get("api_key_header") or "X-API-Key"
    if mode == McpAuthType.API_KEY:
        key_name = header_name(key_name)
    headers = normalize_headers(data["headers"]) if data.get("headers") is not None else dict(previous.get("headers") or {})
    if mode == McpAuthType.API_KEY and key_name in headers:
        raise ValueError("MCP API Key Header 与自定义 Header 冲突")
    return {
        "auth_type": mode,
        "api_key_header": key_name if mode == McpAuthType.API_KEY else None,
        "headers": headers,
    }


def http_credentials(data: dict, config: dict, previous_secret: str | None = None, previous_config: dict | None = None) -> str | None:
    """空主密钥保留；敏感 Header 合并，null 删除，显式清除删除全部秘密。"""
    key, secrets = decrypt_credentials(previous_secret)
    old_mode = (previous_config or {}).get("auth_type", McpAuthType.BEARER if key else McpAuthType.NONE)
    mode = config.get("auth_type", McpAuthType.BEARER if data.get("bearer_token") else old_mode)
    incoming = data.get("bearer_token") if mode == McpAuthType.BEARER else data.get("api_key") if mode == McpAuthType.API_KEY else None
    if data.get("bearer_token") and mode != McpAuthType.BEARER or data.get("api_key") and mode != McpAuthType.API_KEY:
        raise ValueError("MCP 密钥字段与鉴权方式不一致")
    changes = normalize_headers(data.get("sensitive_headers") or {}, removal=True)
    if data.get("clear_credentials"):
        if incoming or any(value is not None for value in changes.values()):
            raise ValueError("清除凭据与替换凭据不能同时提交")
        key, secrets = None, {}
    else:
        if mode != old_mode:
            key = None
        key = incoming or key
        for name, value in changes.items():
            if value is None:
                secrets.pop(name, None)
            else:
                secrets[name] = value
    if mode == McpAuthType.NONE:
        key = None
    if key and (len(key.encode()) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key)):
        raise ValueError("MCP 密钥须为可打印 ASCII 且不超过 8192 字节")
    assemble_headers(config, encrypt_credentials(key, secrets), require_key=False)
    old_key, old_secrets = decrypt_credentials(previous_secret)
    return previous_secret if key == old_key and secrets == old_secrets else encrypt_credentials(key, secrets)


def assemble_headers(config: dict, secret: str | None, *, require_key: bool = True) -> dict[str, str]:
    """复核固定配置并组装唯一的最终请求头，不允许覆盖 SDK 协议字段。"""
    key, secrets = decrypt_credentials(secret)
    mode = config.get("auth_type", McpAuthType.BEARER if key else McpAuthType.NONE)
    headers = normalize_headers(config.get("headers") or {})
    secrets = normalize_headers(secrets)
    if set(headers) & set(secrets):
        raise ValueError("普通和敏感 MCP Header 名称冲突")
    headers.update(secrets)
    if len(headers) > 16:
        raise ValueError("MCP 自定义 Header 合计最多 16 项")
    if require_key and mode != McpAuthType.NONE and not key:
        raise ValueError("MCP 鉴权密钥尚未配置，请补充后重新发现和发布工具")
    if mode == McpAuthType.BEARER:
        if key:
            headers["authorization"] = f"Bearer {key}"
    elif mode == McpAuthType.API_KEY:
        name = header_name(config.get("api_key_header") or "X-API-Key")
        if name in headers:
            raise ValueError("MCP API Key Header 与自定义 Header 冲突")
        if key:
            headers[name] = key
    elif mode != McpAuthType.NONE:
        raise ValueError("MCP 鉴权方式无效")
    if len(headers) > 17 or sum(len(k.encode()) + len(v.encode()) + 4 for k, v in headers.items()) > 16384:
        raise ValueError("MCP 请求头总量超过 16 KiB 或数量上限")
    return headers
