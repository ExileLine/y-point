# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : audit_event.py

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AuditEvent(CustomBaseModel):
    __table_name__ = "audit_event"
    __table_args__ = {
        "comment": "用户操作审计表",
    }

    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True, comment="操作用户 ID")
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="目标用户 ID")
    action: Mapped[str] = mapped_column(String(64), nullable=False, comment="操作类型")
    subject_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="登录标识哈希")
