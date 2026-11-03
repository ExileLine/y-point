# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:12:00
# @Author  : yangyuexiong
# @File    : tool_execution.py

"""工具执行的数据库并发闸门与短期资源租约。"""

from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel, ShanghaiDateTime


class ToolExecutionGate(CustomBaseModel):
    """通过单行写锁串行化容量判定，避免跨 worker 超卖。"""

    __table_name__ = "tool_execution_gate"
    __table_args__ = (
        {
            "comment": "工具并发容量判定闸门",
        },
    )

    __enable_audit_columns__ = False

    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="闸门锁定版本")


class ToolExecutionLease(CustomBaseModel):
    """保存在进程退出后仍可核对的有界执行租约。"""

    __table_name__ = "tool_execution_lease"
    __table_args__ = (
        {
            "comment": "工具调用与依赖准备资源租约",
        },
    )

    token: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, comment="资源租约令牌")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, index=True, comment="工具调用或依赖准备")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="实际使用用户 ID")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, comment="关联运行 ID，试运行或准备时为空")
    expires_at: Mapped[datetime] = mapped_column(ShanghaiDateTime(), nullable=False, index=True, comment="资源租约到期时间")
