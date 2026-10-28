# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : auth_session.py

from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel, ShanghaiDateTime


class AuthSession(CustomBaseModel):
    __table_name__ = "auth_session"
    __enable_auto_id__ = False
    __table_args__ = {
        "comment": "用户登录会话表",
    }

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="会话 ID")
    admin_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属用户 ID")
    refresh_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="刷新令牌哈希")
    expires_at: Mapped[datetime] = mapped_column(ShanghaiDateTime(), nullable=False, comment="会话到期时间")
    revoked_at: Mapped[datetime | None] = mapped_column(ShanghaiDateTime(), nullable=True, comment="会话撤销时间")
