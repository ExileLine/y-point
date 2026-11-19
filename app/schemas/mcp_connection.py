# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:12:01
# @Author  : yangyuexiong
# @File    : mcp_connection.py

"""MCP 共享连接的受限配置与更新请求。"""

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.core.enums import McpTransport


class McpConnectionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="连接名称", examples=["文档服务"])
    transport: McpTransport = Field(description="标准传输方式", examples=["streamable_http"])
    url: str | None = Field(
        default=None, max_length=512, description="HTTP/HTTPS 地址，支持公网、内网、回环及自定义端口", examples=["https://mcp.example.com/mcp"]
    )
    stdio_profile: str | None = Field(default=None, max_length=64, description="管理员配置的 stdio 启动配置名", examples=["local_tools"])
    args: list[Annotated[str, Field(max_length=512)]] = Field(
        default_factory=list,
        max_length=32,
        description="追加到固定启动配置的参数",
        examples=[["--read-only"]],
    )
    bearer_token: str | None = Field(default=None, max_length=8192, description="HTTP Bearer 凭据，仅写入", examples=["secret-token"])
    env: dict[str, str] = Field(
        default_factory=dict,
        description="stdio 凭据环境变量，仅允许配置中的键，仅写入",
        examples=[
            {
                "API_KEY": "secret",
            }
        ],
    )
    share_credentials: bool = Field(default=False, strict=True, description="是否允许公开工具使用此连接凭据", examples=[False])
    enabled: bool = Field(default=True, strict=True, description="是否启用连接", examples=[True])

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        """拒绝空白名称，避免出现无法识别的连接配置。"""
        value = value.strip()
        if not value:
            raise ValueError("连接名称不能为空白")
        return value


class McpConnectionUpdateIn(McpConnectionCreateIn):
    expected_version: int = Field(ge=1, description="预期的当前连接版本，使用完整配置更新", examples=[1])
    clear_credentials: bool = Field(default=False, strict=True, description="清除凭据并撤销历史凭据版本，与新凭据互斥", examples=[False])
