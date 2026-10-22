# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : auth.py

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginIn(BaseModel):
    identifier: str = Field(min_length=3, max_length=256)
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: str


class PasswordResetIn(BaseModel):
    new_password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 900


class UserCreateIn(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{2,63}$")
    mail: EmailStr | None = None
    password: str
    nickname: str | None = Field(default=None, max_length=128)
    role: Literal["admin", "user"] = "user"


class UserUpdateIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=128)
    mail: EmailStr | None = None
    status: Literal[0, 1] | None = None
    role: Literal["admin", "user"] | None = None


class ProfileUpdateIn(BaseModel):
    nickname: str | None = Field(default=None, max_length=128)
    mail: EmailStr | None = None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str | None
    mail: str | None
    nickname: str | None
    role: str
    status: int | None
    last_login_at: datetime | None
    create_time: datetime


class UserPage(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    page_size: int


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    actor_id: int | None
    target_id: int | None
    action: str
    create_time: datetime
