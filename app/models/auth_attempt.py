# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : auth_attempt.py

from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel, ShanghaiDateTime


class AuthAttempt(CustomBaseModel):
    __table_name__ = "auth_attempt"
    __enable_auto_id__ = False
    __table_args__ = {"comment": "登录失败次数表"}

    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="账号或来源地址哈希")
    failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="窗口内失败次数")
    window_started_at: Mapped[datetime] = mapped_column(ShanghaiDateTime(), nullable=False, comment="计数窗口开始时间")
    locked_until: Mapped[datetime | None] = mapped_column(ShanghaiDateTime(), nullable=True, comment="限制登录截止时间")
