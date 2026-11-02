# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : mcp_tools.py

"""通过 MCP Streamable HTTP 发现和调用远端工具。"""

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.services.user_tools import validate_mcp_url


class _PinnedPublicTransport(httpx.AsyncBaseTransport):
    """将 HTTPS 连接固定到校验过的公网 IP，保留原始域名用于 TLS 校验。"""

    def __init__(self, host: str, address: str) -> None:
        """保存本次域名和已验证公网地址。"""
        self.host = host
        self.address = address
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
        headers=headers,
        timeout=httpx.Timeout(10, read=read_timeout),
        transport=_PinnedPublicTransport(urlsplit(url).hostname, address),
        trust_env=False,
        follow_redirects=False,
    ) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            yield streams


async def discover_mcp_tools(url: str, bearer_token: str | None = None) -> list[dict]:
    """建立连接并返回远端可发布工具的名称、说明和输入结构。"""
    async with _mcp_connection(url, bearer_token, 10) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            return [
                {
                    "name": item.name,
                    "description": item.description or "",
                    "input_schema": item.inputSchema,
                }
                for item in response.tools
            ]


async def invoke_mcp_tool(
    url: str, name: str, arguments: dict, bearer_token: str | None, timeout_seconds: int, expected_schema: dict
) -> str:
    """核对远端工具结构后调用，并仅返回有界的文本或结构化内容。"""
    async with _mcp_connection(url, bearer_token, timeout_seconds) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            discovered = await session.list_tools()
            remote = next((item for item in discovered.tools if item.name == name), None)
            if remote is None or remote.inputSchema != expected_schema:
                raise ValueError("MCP 工具结构已变化，请发布新版本")
            response = await session.call_tool(name, arguments=arguments, read_timeout_seconds=timedelta(seconds=timeout_seconds))
            result = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
            if len(result.encode()) > 64 * 1024:
                raise ValueError("MCP 结果超过 64 KiB")
            if response.isError:
                raise ValueError("MCP 工具执行失败")
            return result
