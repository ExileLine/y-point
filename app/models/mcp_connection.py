# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:12:01
# @Author  : yangyuexiong
# @File    : mcp_connection.py

"""复用 MCP 连接及其当前发布策略，执行配置由 Tool 版本固定。"""

from sqlalchemy import Boolean, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

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
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密的 Bearer 或环境变量凭据")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否允许发现及执行")
    share_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否允许公开工具借用连接凭据")
    credentials_revoked_through: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已撤销凭据的最大连接版本")
