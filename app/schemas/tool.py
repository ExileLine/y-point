# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : tool.py

"""用户 Tool 创建、更新及试运行的接口结构。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import ScriptLanguage, ToolKind, Visibility


class ToolCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="创建者范围内的工具名称", examples=["disk_report"])
    description: str = Field(min_length=1, max_length=1024, description="模型可见的工具用途", examples=["读取磁盘使用摘要"])
    kind: ToolKind = Field(description="脚本或 MCP 工具", examples=["script"])
    visibility: Visibility = Field(default=Visibility.PRIVATE, description="私有或公开", examples=["private"])
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description="脚本参数的 JSON Schema 对象；MCP 由远端发现",
        examples=[
            {
                "type": "object",
            }
        ],
    )
    script_language: ScriptLanguage | None = Field(default=None, description="脚本语言 python 或 bash", examples=["python"])
    requirements: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Python 依赖清单，支持包名与版本约束，不接受 URL 或 pip 选项",
        examples=[["httpx>=0.27,<1"]],
    )
    source_code: str | None = Field(default=None, description="UTF-8 脚本源码", examples=["import json\nprint(json.dumps({'ok': True}))"])
    mcp_url: str | None = Field(
        default=None, max_length=512, description="MCP Streamable HTTP 地址", examples=["https://mcp.example.com/mcp"]
    )
    mcp_tool_name: str | None = Field(default=None, max_length=128, description="远端 MCP 工具名", examples=["search"])
    mcp_bearer_token: str | None = Field(default=None, description="MCP Bearer 凭据，仅写入时接收", examples=["token-value"])
    share_credentials: bool = Field(default=False, description="公开 MCP 工具时明确允许他人使用该凭据", examples=[False])


class ToolUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="预期的当前 Tool 版本号", examples=[1])
    description: str | None = Field(default=None, min_length=1, max_length=1024, description="新版工具用途", examples=["读取磁盘摘要"])
    visibility: Visibility | None = Field(default=None, description="私有或公开", examples=["public"])
    enabled: bool | None = Field(default=None, description="是否启用", examples=[True])
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description="新版参数 JSON Schema",
        examples=[
            {
                "type": "object",
            }
        ],
    )
    requirements: list[str] | None = Field(
        default=None, max_length=32, description="新版 Python 依赖清单，空列表表示不需要额外依赖", examples=[["httpx==0.28.1"]]
    )
    source_code: str | None = Field(default=None, description="新版脚本源码", examples=["print('{}')"])
    mcp_url: str | None = Field(default=None, max_length=512, description="新版 MCP 地址", examples=["https://mcp.example.com/mcp"])
    mcp_tool_name: str | None = Field(default=None, max_length=128, description="新版远端工具名", examples=["search"])
    mcp_bearer_token: str | None = Field(default=None, description="替换 MCP Bearer 凭据，留空时保留", examples=["new-token"])
    share_credentials: bool | None = Field(default=None, description="公开时是否授权共享凭据", examples=[True])


class ToolTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(
        description="试运行参数",
        examples=[
            {
                "path": "/",
            }
        ],
    )


class McpDiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(max_length=512, description="MCP Streamable HTTP 地址", examples=["https://mcp.example.com/mcp"])
    bearer_token: str | None = Field(default=None, description="可选的 MCP Bearer 凭据", examples=["token-value"])


class ToolEnvironmentRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="待重新准备环境的当前 Tool 版本号", examples=[1])
