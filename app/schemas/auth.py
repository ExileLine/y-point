# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : auth.py

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginIn(BaseModel):
    identifier: str = Field(min_length=3, max_length=256, description="用户名或邮箱", examples=["admin@example.com"])
    password: str = Field(description="登录密码", examples=["ExamplePassword123!"])


class RefreshIn(BaseModel):
    refresh_token: str = Field(description="用于换取新令牌的刷新令牌", examples=["eyJhbGciOi...example"])


class PasswordChangeIn(BaseModel):
    current_password: str = Field(description="当前密码", examples=["ExamplePassword123!"])
    new_password: str = Field(description="新密码", examples=["NewExamplePassword456!"])


class PasswordResetIn(BaseModel):
    new_password: str = Field(description="重置后的新密码", examples=["NewExamplePassword456!"])


class TokenOut(BaseModel):
    access_token: str = Field(description="访问令牌", examples=["eyJhbGciOi...access-example"])
    refresh_token: str = Field(description="刷新令牌", examples=["eyJhbGciOi...refresh-example"])
    token_type: str = Field(default="bearer", description="令牌类型", examples=["bearer"])
    expires_in: int = Field(description="访问令牌有效期（秒）", examples=[604800])


class UserCreateIn(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{2,63}$", description="用户名", examples=["alice_01"])
    mail: EmailStr | None = Field(default=None, description="邮箱地址", examples=["alice@example.com"])
    password: str = Field(description="初始密码", examples=["ExamplePassword123!"])
    nickname: str | None = Field(default=None, max_length=128, description="昵称", examples=["Alice"])
    role: Literal["admin", "user"] = Field(default="user", description="用户角色", examples=["user"])


class UserUpdateIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=128, description="昵称", examples=["Alice"])
    mail: EmailStr | None = Field(default=None, description="邮箱地址", examples=["alice@example.com"])
    status: Literal[0, 1] | None = Field(default=None, description="状态：1 启用，0 停用", examples=[1])
    role: Literal["admin", "user"] | None = Field(default=None, description="用户角色", examples=["user"])


class ProfileUpdateIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=128, description="昵称", examples=["Alice"])
    mail: EmailStr | None = Field(default=None, description="邮箱地址", examples=["alice@example.com"])


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="用户 ID", examples=[1])
    username: str | None = Field(description="用户名", examples=["alice_01"])
    mail: str | None = Field(description="邮箱地址", examples=["alice@example.com"])
    nickname: str | None = Field(description="昵称", examples=["Alice"])
    role: str = Field(description="用户角色", examples=["user"])
    status: int | None = Field(description="状态：1 启用，0 停用", examples=[1])
    last_login_at: datetime | None = Field(description="最近登录时间", examples=["2026-10-22T10:00:00"])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-21T16:46:46"])


class UserPage(BaseModel):
    items: list[UserOut] = Field(
        description="当前页的用户列表",
        examples=[[
            {
                "id": 1, "username": "alice_01", "mail": "alice@example.com", "nickname": "Alice",
                "role": "user", "status": 1, "last_login_at": "2026-10-22T10:00:00", "create_time": "2026-10-21T16:46:46",
            }
        ]],
    )
    total: int = Field(description="用户总数", examples=[36])
    page: int = Field(description="当前页码", examples=[1])
    page_size: int = Field(description="每页数量", examples=[20])


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="审计事件 ID", examples=[1])
    actor_id: int | None = Field(description="操作人 ID", examples=[1])
    target_id: int | None = Field(description="操作对象 ID", examples=[2])
    action: str = Field(description="操作类型", examples=["auth.password_changed"])
    create_time: datetime = Field(description="事件发生时间", examples=["2026-10-22T10:00:00"])
