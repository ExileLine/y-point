# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:12:01
# @Author  : yangyuexiong
# @File    : mcp_connection.py

"""复用 MCP 连接及其当前发布策略，执行配置由 Tool 版本固定。"""

from sqlalchemy import Boolean, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ToolApprovalMode
from app.models.base import CustomBaseModel


class McpConnection(CustomBaseModel):
    __table_name__ = "mcp_connection"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_yp_mcp_connection_owner_name"),
        {
            "comment": "可由多个工具复用的 MCP 连接",
        },
    )

    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="连接所有者 ID")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="所有者范围内的连接名称")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="当前连接配置版本")
    config: Mapped[dict] = mapped_column(JSON, nullable=False, comment="不含凭据的传输与受控执行配置")
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密的 HTTP 密钥、敏感 Headers 或 stdio 环境变量")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否允许发现及执行")
    share_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否允许公开工具借用连接凭据")
    approval_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=ToolApprovalMode.WRITES, comment="连接默认审批方式")
    credentials_revoked_through: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已撤销凭据的最大连接版本")

    config_revoked_through: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已撤销数据范围配置的最大连接版本")


class McpOperation(CustomBaseModel):
    __table_name__ = "mcp_operation"
    __table_args__ = (
        UniqueConstraint("owner_id", "kind", "target_id", "request_id", name="uq_yp_mcp_operation_request"),
        {
            "comment": "MCP 同步和 Agent 版本升级的幂等回执",
        },
    )

    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="实际确认操作的用户 ID")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, comment="连接同步或 Agent 工具升级")
    target_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="连接或 Agent ID")
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="客户端稳定重试 ID")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="确认请求摘要")
    result: Mapped[dict] = mapped_column(JSON, nullable=False, comment="逐项操作结果，不含凭据与内部连接配置")


class McpConnectionShare(CustomBaseModel):
    __table_name__ = "mcp_connection_share"
    __table_args__ = (
        UniqueConstraint("connection_id", "user_id", name="uq_yp_mcp_connection_share_user"),
        {
            "comment": "指定用户的连接摘要、工具子集及凭据借用授权",
        },
    )

    connection_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="共享连接 ID")
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="获授权用户 ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="共享授权版本，用于并发修改检查")
    tool_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="明确授权的已发布工具 ID，不包含未来新工具")
    allow_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否允许在授权工具中借用凭据，不返回密钥")


class AgentMcpTool(CustomBaseModel):
    __table_name__ = "agent_mcp_tool"
    __table_args__ = (
        UniqueConstraint("agent_id", "connection_id", "tool_id", name="uq_yp_agent_mcp_tool_binding"),
        {
            "comment": "Agent 按连接明确选择的工具及固定版本",
        },
    )

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="Agent ID")
    connection_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="关联连接 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="明确选择的已发布工具 ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="固定工具版本，升级须显式确认")
