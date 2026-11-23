# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:12:01
# @Author  : yangyuexiong
# @File    : mcp_connection.py

"""MCP 共享连接的受限配置与更新请求。"""

from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.core.enums import McpAuthType, McpSyncAction, McpTransport, RecoveryPolicy, ToolApprovalMode, ToolEffect


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
    auth_type: McpAuthType | None = Field(default=None, description="HTTP 鉴权方式，省略保留旧配置", examples=["api_key"])
    api_key: str | None = Field(default=None, max_length=8192, description="API Key，仅写入，空值保留", examples=["secret-key"])
    api_key_header: str | None = Field(default=None, max_length=64, description="API Key 使用的 Header 名", examples=["X-API-Key"])
    headers: dict[str, str] | None = Field(
        default=None,
        description="普通 HTTP Headers；省略保留，空对象清空",
        examples=[
            {
                "X-Tenant": "demo",
            }
        ],
    )
    sensitive_headers: dict[str, str | None] = Field(
        default_factory=dict,
        description="敏感 HTTP Headers；省略保留，值为 null 删除该项",
        examples=[
            {
                "X-Secret": "secret",
            }
        ],
    )
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

    approval_mode: ToolApprovalMode = Field(default=ToolApprovalMode.WRITES, description="连接默认审批方式，不接受 inherit", examples=["writes"])

    @field_validator("approval_mode")
    @classmethod
    def validate_approval_mode(cls, value: ToolApprovalMode) -> ToolApprovalMode:
        """连接本身须声明默认方式，不能向不存在的上级继承。"""
        if value == ToolApprovalMode.INHERIT:
            raise ValueError("连接审批方式不能为 inherit")
        return value

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


class McpSyncSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_name: str = Field(min_length=1, max_length=128, description="预览中的远端原始函数名", examples=["search"])
    tool_id: int | None = Field(default=None, ge=1, description="已有 Tool ID；空值创建私有工具", examples=[1])
    expected_version: int | None = Field(default=None, ge=1, description="预览中的本地 Tool 版本", examples=[1])
    action: McpSyncAction = Field(default=McpSyncAction.PUBLISH, description="发布或停用已移除工具", examples=["publish"])
    display_name: str | None = Field(default=None, min_length=1, max_length=128, description="新工具显示名称；不改变已有技术标识", examples=["工单查询"])
    read_only_reason: str | None = Field(
        default=None, min_length=1, max_length=512, description="执行变化后的只读重新确认理由", examples=["已核对接口仅执行查询"]
    )
    effect: ToolEffect | None = Field(default=None, description="确认未知工具的动作性质", examples=["write"])
    recovery_policy: RecoveryPolicy | None = Field(default=None, description="新版本恢复策略", examples=["manual"])

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        """批量发布遵守与单个 Tool 相同的可读名称规则。"""
        if value is not None:
            value = value.strip()
            if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Tool 显示名称不能为空或包含控制字符")
        return value


class McpSyncIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64, description="稳定重试 ID；更改选择需使用新 ID", examples=["sync-001"])
    expected_version: int = Field(ge=1, description="预览中的连接版本", examples=[2])
    preview_hash: str = Field(min_length=64, max_length=64, description="预览摘要，发现或本地状态变化须重新预览", examples=["a" * 64])
    selections: list[McpSyncSelection] = Field(
        min_length=1,
        max_length=128,
        description="明确确认的工具子集",
        examples=[
            [
                {
                    "remote_name": "search",
                    "action": "publish",
                }
            ]
        ],
    )


class AgentToolUpgradeSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_id: int = Field(ge=1, description="已关联的 Tool ID", examples=[1])
    expected_version: int = Field(ge=1, description="Agent 当前固定的工具版本", examples=[1])
    target_version: int = Field(ge=1, description="预览中的目标已发布版本", examples=[2])


class AgentToolUpgradeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64, description="稳定重试 ID", examples=["upgrade-001"])
    expected_version: int = Field(ge=1, description="Agent 配置期望版本", examples=[2])
    preview_hash: str = Field(min_length=64, max_length=64, description="升级预览摘要", examples=["a" * 64])
    selections: list[AgentToolUpgradeSelection] = Field(
        min_length=1,
        max_length=128,
        description="明确选择的工具版本升级",
        examples=[
            [
                {
                    "tool_id": 1,
                    "expected_version": 1,
                    "target_version": 2,
                }
            ]
        ],
    )


class McpConnectionShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, description="首次授权为 0，修改使用已读取的授权版本", examples=[0])
    tool_ids: list[int] = Field(description="明确允许的已发布工具子集，空列表仅共享摘要", examples=[[1, 2]])
    allow_credentials: bool = Field(default=False, description="是否允许指定用户借用授权工具的凭据，永不返回密钥")
