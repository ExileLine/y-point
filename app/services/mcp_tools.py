# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : mcp_tools.py

"""通过 MCP Streamable HTTP 发现和调用远端工具。"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, McpError
from mcp.client.streamable_http import streamable_http_client

from app.core.config import get_config
from app.services.user_tools import validate_mcp_url


class _BoundedResponseStream(httpx.AsyncByteStream):
    """在字节进入 SDK 解析器前执行连接级接收预算。"""

    def __init__(self, stream: httpx.AsyncByteStream, transport: "_PinnedPublicTransport") -> None:
        """保留底层流及多个响应共用的接收计数。"""
        self.stream = stream
        self.transport = transport

    async def __aiter__(self):
        """超限数据不向上游解析器交付，并始终关闭底层流。"""
        try:
            async for chunk in self.stream:
                self.transport.received += len(chunk)
                if self.transport.received > self.transport.max_bytes:
                    raise ValueError("MCP 传输超过接收字节上限")
                yield chunk
        finally:
            await self.stream.aclose()

    async def aclose(self) -> None:
        """关闭原始 HTTP 响应。"""
        await self.stream.aclose()


class _PinnedPublicTransport(httpx.AsyncBaseTransport):
    """将 HTTPS 连接固定到校验过的公网 IP，保留原始域名用于 TLS 校验。"""

    def __init__(self, host: str, address: str) -> None:
        """保存本次域名和已验证公网地址。"""
        self.host = host
        self.address = address
        self.received = 0
        self.max_bytes = get_config().MCP_RESPONSE_MAX_BYTES
        self.transport = httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """拒绝跨域请求并固定目标地址与 TLS SNI。"""
        if request.url.scheme != "https" or request.url.host != self.host or request.url.port not in (None, 443):
            raise ValueError("MCP 请求目标已变化")
        address = f"[{self.address}]" if ":" in self.address else self.address
        target = request.url.copy_with(host=address, port=443)
        headers = request.headers.copy()
        headers["Host"] = self.host
        rewritten = httpx.Request(
            request.method,
            target,
            headers=headers,
            stream=request.stream,
            extensions={
                **request.extensions,
                "sni_hostname": self.host,
            },
        )
        response = await self.transport.handle_async_request(rewritten)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ValueError("MCP 不允许重定向")
        encoding = response.headers.get("content-encoding", "identity").lower()
        length = response.headers.get("content-length")
        if encoding != "identity" or (length is not None and (not length.isdigit() or int(length) > self.max_bytes - self.received)):
            await response.aclose()
            raise ValueError("MCP 响应编码或声明长度超过接收限制")
        response.stream = _BoundedResponseStream(response.stream, self)
        return response

    async def aclose(self) -> None:
        """关闭底层 HTTP 连接池。"""
        await self.transport.aclose()


@asynccontextmanager
async def _mcp_connection(url: str, bearer_token: str | None, read_timeout: int):
    """管理固定公网 IP 的 HTTP 客户端及 MCP 连接生命周期。"""
    address = validate_mcp_url(url)
    headers = (
        {
            "Authorization": f"Bearer {bearer_token}",
        }
        if bearer_token
        else None
    )
    async with httpx.AsyncClient(
        headers={
            **(headers or {}),
            "Accept-Encoding": "identity",
        },
        timeout=httpx.Timeout(get_config().MCP_CONNECT_TIMEOUT_SECONDS, read=read_timeout),
        transport=_PinnedPublicTransport(urlsplit(url).hostname, address),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            yield streams


async def _discover_session(session: ClientSession) -> list[dict]:
    """有界分页发现，校验数量、名称、描述、Schema 与结果总大小。"""
    config = get_config()
    tools = []
    names = set()
    cursors = set()
    cursor = None
    total_bytes = 2
    for _ in range(config.MCP_DISCOVERY_MAX_TOOLS + 1):
        response = await session.list_tools(cursor=cursor)
        for item in response.tools:
            description = item.description or ""
            schema_size = len(json.dumps(item.inputSchema, ensure_ascii=False).encode())
            if len(item.name.encode()) > 128 or len(description.encode()) > 4096 or schema_size > 16 * 1024:
                raise ValueError("MCP 工具名称、描述或参数结构超限")
            if item.name in names:
                raise ValueError("MCP 发现包含重复工具名")
            names.add(item.name)
            tool = {
                "name": item.name,
                "description": description,
                "input_schema": item.inputSchema,
                "read_only": item.annotations.readOnlyHint if item.annotations is not None else None,
            }
            total_bytes += len(json.dumps(tool, ensure_ascii=False).encode()) + 2
            if len(tools) >= config.MCP_DISCOVERY_MAX_TOOLS or total_bytes > config.MCP_DISCOVERY_MAX_BYTES:
                raise ValueError("MCP 发现工具数量或总字节超限")
            tools.append(tool)
        cursor = response.nextCursor
        if cursor is None:
            return tools
        if cursor in cursors:
            raise ValueError("MCP 发现分页游标重复")
        cursors.add(cursor)
    raise ValueError("MCP 发现分页超限")


def _is_mcp_timeout(exc: Exception) -> bool:
    """识别 SDK 或任务组包装的超时，保持上层工具超时及外部写结果不明语义。"""
    if isinstance(exc, ExceptionGroup):
        return all(isinstance(item, Exception) and _is_mcp_timeout(item) for item in exc.exceptions)
    return isinstance(exc, (TimeoutError, httpx.TimeoutException)) or isinstance(exc, McpError) and exc.error.code == 408


@asynccontextmanager
async def _mcp_deadline(seconds: int):
    """连接、握手、分页和调用共享单次时限，SDK 的进度通知不延长它。"""
    try:
        async with asyncio.timeout(seconds):
            yield
    except Exception as exc:
        if _is_mcp_timeout(exc):
            raise TimeoutError("MCP 单次操作超时") from None
        raise


async def discover_mcp_tools(url: str, bearer_token: str | None = None) -> list[dict]:
    """在配置的完整连接时限和传输预算内发现远端可发布工具。"""
    timeout = get_config().MCP_DISCOVERY_TIMEOUT_SECONDS
    async with _mcp_deadline(timeout):
        async with _mcp_connection(url, bearer_token, timeout) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                return await _discover_session(session)


async def invoke_mcp_tool(
    url: str,
    name: str,
    arguments: dict,
    bearer_token: str | None,
    timeout_seconds: int,
    expected_schema: dict,
    *,
    expected_read_only: bool = False,
) -> str:
    """在单次预算内连接、核对远端工具结构并调用，返回有界内容。"""
    timeout = min(timeout_seconds, get_config().MCP_CALL_TIMEOUT_SECONDS)
    async with _mcp_deadline(timeout):
        async with _mcp_connection(url, bearer_token, timeout) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session:
                await session.initialize()
                discovered = await _discover_session(session)
                remote = next((item for item in discovered if item["name"] == name), None)
                if remote is None or remote["input_schema"] != expected_schema or expected_read_only and remote["read_only"] is False:
                    raise ValueError("MCP 工具结构已变化，请发布新版本")
                response = await session.call_tool(name, arguments=arguments, read_timeout_seconds=timedelta(seconds=timeout))
                result = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
                if len(result.encode()) > 64 * 1024:
                    raise ValueError("MCP 结果超过 64 KiB")
                if response.isError:
                    raise ValueError("MCP 工具执行失败")
                return result
