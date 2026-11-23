# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : run_mcp_resource.py

"""仅供 Worker 回收远端 MCP 会话的加密资源凭单，不对模型或公开接口暴露。"""

from datetime import datetime
from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import CustomBaseModel, ShanghaiDateTime


class RunMcpResource(CustomBaseModel):
    __table_name__ = "run_mcp_resource"
    __enable_auto_id__ = False

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="远端会话回收凭单 ID")
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="所属业务 Run")
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False, comment="以部署密钥加密的回收地址、凭据、会话 ID 和协议版本")
    retired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="本地会话已退出但远端尚未确认删除")
    expires_at: Mapped[datetime] = mapped_column(ShanghaiDateTime(), nullable=False, comment="会话最长存活截止时间")
    next_attempt_at: Mapped[datetime] = mapped_column(ShanghaiDateTime(), nullable=False, comment="下次尽力回收时间")
