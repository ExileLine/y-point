# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : tool.py

"""用户 Tool、不可变版本及 Agent/Run 调用关系。"""

from sqlalchemy import Boolean, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class UserTool(CustomBaseModel):
    __table_name__ = "user_tool"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_yp_user_tool_owner_name"),
        {
            "comment": "用户创建的脚本或 MCP Tool",
        },
    )

    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="创建用户 ID")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="创建者范围内的 Tool 名称")
    description: Mapped[str] = mapped_column(String(1024), nullable=False, comment="模型可见的 Tool 用途说明")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="脚本或 MCP 来源类型")
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, comment="私有或公开")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="当前发布版本号")


class UserToolVersion(CustomBaseModel):
    __table_name__ = "user_tool_version"
    __table_args__ = (
        UniqueConstraint("tool_id", "version", name="uq_yp_user_tool_version_number"),
        {
            "comment": "用户 Tool 不可变版本",
        },
    )

    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="Tool ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="执行定义 SHA256")
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, comment="工具输入 JSON Schema")
    effect: Mapped[str] = mapped_column(String(16), nullable=False, comment="只读或外部写入")
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="调用前是否要求审批")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="当前版本是否通过发布验证")
    script_language: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="脚本语言")
    source_code: Mapped[str | None] = mapped_column(Text, nullable=True, comment="脚本源代码")
    mcp_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="MCP Streamable HTTP 地址")
    mcp_tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="远端 MCP 工具名")
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密的 MCP 连接凭据")
    share_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="公开时是否授权共享连接凭据")


class AgentTool(CustomBaseModel):
    __table_name__ = "agent_tool"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_id", name="uq_yp_agent_tool_pair"),
        {
            "comment": "智能体直接关联的 Tool 版本",
        },
    )

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="智能体 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="固定 Tool 版本号")


class RunTool(CustomBaseModel):
    __table_name__ = "run_tool"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_id", name="uq_yp_run_tool_pair"),
        {
            "comment": "运行可调用的 Tool 版本快照",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="运行固定版本号")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="运行固定执行定义摘要")


class ToolInvocation(CustomBaseModel):
    __table_name__ = "tool_invocation"
    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_yp_tool_invocation_call"),
        {
            "comment": "用户 Tool 调用审计记录",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    call_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="模型工具调用 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="调用 Tool 版本号")
    state: Mapped[str] = mapped_column(String(24), nullable=False, comment="调用状态")
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="输入参数 SHA256")
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="脱敏失败类别")
