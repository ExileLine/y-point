# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : security.py

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.auth_session import AuthSession


ACCESS_TOKEN_SECONDS = 7 * 24 * 60 * 60
REFRESH_DAYS = 30
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


def utc_now() -> datetime:
    """返回用于令牌和会话有效期比较的 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def validate_password(password: str) -> None:
    """校验新密码的长度和字符组成。"""
    if (
        len(password) < 12
        or len(password.encode()) > 72
        or not any(c.isupper() for c in password)
        or not any(c.islower() for c in password)
        or not any(c.isdigit() for c in password)
    ):
        raise CustomException(status_code=400, custom_code=10005, detail="密码需为 12-72 位，且包含大小写字母和数字")


def new_refresh_token() -> tuple[str, str]:
    """生成刷新令牌原文及供数据库保存的摘要。"""
    token = secrets.token_urlsafe(48)
    return token, hashlib.sha256(token.encode()).hexdigest()


def make_access_token(admin: Admin, session_id: str) -> str:
    """为指定用户和会话签发访问令牌。"""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(admin.id),
            "sid": session_id,
            "ver": admin.token_version,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(seconds=ACCESS_TOKEN_SECONDS),
            "jti": str(uuid4()),
        },
        get_config().SECRET_KEY,
        algorithm="HS256",
    )


def unauthorized() -> CustomException:
    """构造统一格式的未授权业务异常。"""
    return CustomException(status_code=401, custom_code=401, detail="未授权或令牌已失效")


async def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db_session),
) -> Admin:
    """校验 Bearer 令牌、用户状态及数据库会话，并返回当前用户。"""
    if credentials is None:
        raise unauthorized()
    try:
        payload = jwt.decode(
            credentials.credentials,
            get_config().SECRET_KEY,
            algorithms=["HS256"],
            options={"require": ["sub", "sid", "ver", "type", "iat", "exp"]},
        )
        if payload["type"] != "access" or type(payload["ver"]) is not int or type(payload["exp"]) is not int:
            raise unauthorized()
        admin_id = int(payload["sub"])
        session_id = payload["sid"]
        if not isinstance(session_id, str) or not session_id:
            raise unauthorized()
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise unauthorized() from None

    admin = await db.get(Admin, admin_id)
    auth_session = await db.get(AuthSession, session_id)
    if (
        admin is None
        or admin.status != 1
        or admin.is_deleted != 0
        or admin.token_version != payload["ver"]
        or auth_session is None
        or auth_session.admin_id != admin_id
        or auth_session.revoked_at is not None
        or auth_session.expires_at <= utc_now()
    ):
        raise unauthorized()
    request.state.auth_session_id = session_id
    request.state.auth_token_version = payload["ver"]
    request.state.auth_token_expires_at = payload["exp"]
    return admin


async def require_admin(admin: Admin = Depends(get_current_admin)) -> Admin:
    """检查当前用户是否具有管理员角色。"""
    if admin.role != "admin":
        raise CustomException(status_code=403, custom_code=403, detail="需要管理员权限")
    return admin


def authorize_owner(admin: Admin, owner_id: int) -> None:
    """仅允许资源所有者或管理员访问指定资源。"""
    if admin.role != "admin" and admin.id != owner_id:
        raise CustomException(status_code=404, custom_code=10002, detail="资源不存在")


check_admin_existence = get_current_admin
