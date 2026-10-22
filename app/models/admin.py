# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : admin.py

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.password import hash_password, verify_password
from app.models.base import CustomBaseModel


class Admin(CustomBaseModel):
    __table_name__ = "admin"
    __table_args__ = (
        Index("uq_yp_admin_username", "username", unique=True),
        Index("uq_yp_admin_mail", "mail", unique=True),
        {"comment": "用户表"},
    )

    username: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="用户名")
    password: Mapped[str] = mapped_column(String(255), nullable=False, comment="密码")
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="昵称")
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="手机号")
    mail: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="邮箱")
    code: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="用户编号")
    seat: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="座位编号")
    department: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="部门")
    position: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="职位")
    superior: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="上级")
    login_type: Mapped[str | None] = mapped_column(String(64), nullable=True, default="single", comment="登录类型:single;many")
    is_tourist: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="0-游客账户;1-非游客账户")
    creator: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="创建人")
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="创建人id")
    modifier: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="更新人")
    modifier_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, comment="更新人id")
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="备注")
    role: Mapped[str] = mapped_column(String(16), default="user", nullable=False, comment="用户角色：admin 管理员，user 普通用户")
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="令牌版本")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="最近登录时间")

    async def set_password(self, raw_password: str):
        """设置当前用户的密码哈希。"""
        self.password = hash_password(raw_password)

    async def verify_password(self, raw_password: str):
        """校验当前用户的原始密码。"""
        return verify_password(raw_password, self.password)
