# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : tool.py

"""用户 Tool 创建、更新及试运行的接口结构。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import RecoveryPolicy, ScriptLanguage, ToolEffect, ToolKind, Visibility


class _ToolDisplayNameIn(BaseModel):
    """创建与更新工具共用的可读显示名称。"""

    display_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="创建者范围内唯一的工具显示名称，支持中文、空格及大小写",
        examples=["天气查询 Weather"],
    )

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value):
        """去除名称两端空白，拒绝空名称和不可见控制字符。"""
        if isinstance(value, str):
            value = value.strip()
            if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
                raise ValueError("Tool 显示名称不能为空或包含控制字符")
        return value


class ToolCreateIn(_ToolDisplayNameIn):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64, description="不可变内部调用标识，未提供时自动生成", examples=["disk_report"])

    @model_validator(mode="after")
    def require_tool_name(self):
        """新客户端填写显示名称，已有客户端可继续提交内部名称。"""
        if self.display_name is None and self.name is None:
            raise ValueError("请填写 Tool 显示名称")
        return self

    description: str = Field(min_length=1, max_length=1024, description="模型可见的工具用途", examples=["读取磁盘使用摘要"])
    kind: ToolKind = Field(description="脚本或 MCP 工具", examples=["script"])
    visibility: Visibility = Field(default=Visibility.PRIVATE, description="私有或公开", examples=["private"])
    input_schema: dict[str, Any] | None = Field(
        default=None,
        description="脚本参数的 JSON Schema 对象；MCP 由远端发现",
        examples=[
            {
                "type": "object",
            },
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
    mcp_connection_id: int | None = Field(default=None, ge=1, description="复用的 MCP 连接 ID，发布时固定当前连接版本", examples=[1])
    mcp_url: str | None = Field(
        default=None,
        max_length=512,
        description="旧 HTTP 工具兼容地址，新建工具使用 mcp_connection_id",
        deprecated=True,
        examples=["https://mcp.example.com/mcp"],
    )
    mcp_tool_name: str | None = Field(default=None, max_length=128, description="远端 MCP 工具名", examples=["search"])
    mcp_bearer_token: str | None = Field(default=None, description="MCP Bearer 凭据，仅写入时接收", examples=["token-value"])
    share_credentials: bool = Field(default=False, description="公开 MCP 工具时明确允许他人使用该凭据", examples=[False])
    requires_approval: bool = Field(default=False, strict=True, description="工具调用前是否需要人工审批，默认不需要", examples=[False])
    effect: ToolEffect | None = Field(default=None, description="MCP 读写属性；远端未提供声明时仅管理员可确认只读", examples=["read"])
    recovery_policy: RecoveryPolicy | None = Field(default=None, description="恢复策略，外部写需配置可核对的幂等契约", examples=["idempotent"])
    idempotency_key_field: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="服务端注入幂等键的顶层字符串参数名",
        examples=["request_id"],
    )


class ToolUpdateIn(_ToolDisplayNameIn):
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
            },
        ],
    )
    requirements: list[str] | None = Field(
        default=None,
        max_length=32,
        description="新版 Python 依赖清单，空列表表示不需要额外依赖",
        examples=[["httpx==0.28.1"]],
    )
    source_code: str | None = Field(default=None, description="新版脚本源码", examples=["print('{}')"])
    mcp_connection_id: int | None = Field(default=None, ge=1, description="新版工具采用的 MCP 连接 ID，重新固定该连接当前版本", examples=[1])
    mcp_url: str | None = Field(
        default=None,
        max_length=512,
        description="旧 HTTP 工具兼容地址，共享连接在连接管理中修改",
        deprecated=True,
        examples=["https://mcp.example.com/mcp"],
    )
    mcp_tool_name: str | None = Field(default=None, max_length=128, description="新版远端工具名", examples=["search"])
    mcp_bearer_token: str | None = Field(default=None, description="替换 MCP Bearer 凭据，留空时保留", examples=["new-token"])
    share_credentials: bool | None = Field(default=None, description="公开时是否授权共享凭据", examples=[True])
    clear_mcp_credentials: bool = Field(default=False, description="清除新版本凭据并撤销历史凭据版本，与替换密钥互斥", examples=[True])
    requires_approval: bool | None = Field(default=None, strict=True, description="新版工具调用前是否需要审批；省略时保留原设置", examples=[False])
    effect: ToolEffect | None = Field(default=None, description="新版 MCP 读写属性", examples=["read"])
    recovery_policy: RecoveryPolicy | None = Field(default=None, description="新版调用恢复策略", examples=["idempotent"])
    idempotency_key_field: str | None = Field(default=None, min_length=1, max_length=128, description="远端幂等键字符串参数名", examples=["request_id"])


class ToolTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arguments: dict[str, Any] = Field(
        description="试运行参数",
        examples=[
            {
                "path": "/",
            },
        ],
    )


class McpDiscoverIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        max_length=512,
        description="旧 HTTP 工具兼容地址，新建工具使用 mcp_connection_id",
        deprecated=True,
        examples=["https://mcp.example.com/mcp"],
    )
    bearer_token: str | None = Field(default=None, description="可选的 MCP Bearer 凭据", examples=["token-value"])


class ToolEnvironmentRetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="待重新准备环境的当前 Tool 版本号", examples=[1])


class ToolCredentialRevokeIn(BaseModel):
    """在远端不可用时仍能撤销本地所有历史凭据。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="预期的当前 Tool 版本号", examples=[1])


class ToolWriteContractVerifyIn(BaseModel):
    """管理员对指定外部写工具版本的恢复证据作出验收确认。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="已验证幂等及故障行为的 Tool 版本号", examples=[1])
    evidence_reference: str = Field(
        min_length=8,
        max_length=256,
        description="重复提交与返回丢失验收记录的引用，不填写密钥或正文",
        examples=["docs/m5-write-evidence.md#case-1"],
    )
