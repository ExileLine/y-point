# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : auth.py

import hashlib

from fastapi import APIRouter, Depends, Request
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import ACCESS_TOKEN_SECONDS, get_current_admin, make_access_token, new_refresh_token, unauthorized, utc_now
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.auth_session import AuthSession
from app.schemas.auth import LoginIn, PasswordChangeIn, ProfileUpdateIn, RefreshIn, TokenOut, UserOut
from app.services.identity import (
    audit,
    check_login_limit,
    check_user_password,
    clear_login_limit,
    create_session,
    ensure_unique_identity,
    find_login_user,
    record_login_failure,
    revoke_sessions,
    set_user_password,
)


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", summary="用户登录")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db_session)):
    """校验密码，创建登录会话并返回访问与刷新令牌。"""
    identifier = body.identifier.strip().lower()
    client_ip = request.client.host if request.client else "unknown"
    await check_login_limit(db, identifier, client_ip)
    admin = await find_login_user(db, identifier)
    if not admin or admin.status != 1 or not check_user_password(admin, body.password):
        await record_login_failure(db, identifier, client_ip)
        raise CustomException(status_code=401, custom_code=401, detail="用户名或密码错误")
    await clear_login_limit(db, identifier, client_ip)
    admin.last_login_at = utc_now()
    session, refresh_token = await create_session(db, admin)
    audit(db, "auth.login", actor_id=admin.id, target_id=admin.id)
    await db.commit()
    data = TokenOut(access_token=make_access_token(admin, session.id), refresh_token=refresh_token, expires_in=ACCESS_TOKEN_SECONDS)
    return api_response(data=data.model_dump())


@router.post("/refresh", summary="刷新登录令牌")
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db_session)):
    """原子轮换刷新令牌并签发新的访问令牌。"""
    session_id, sep, secret = body.refresh_token.partition(".")
    if not sep or not session_id or not secret:
        raise unauthorized()
    session = await db.get(AuthSession, session_id)
    if not session or session.revoked_at or session.expires_at <= utc_now():
        raise unauthorized()
    admin = await db.get(Admin, session.admin_id)
    if not admin or admin.status != 1 or admin.is_deleted != 0:
        raise unauthorized()
    presented_hash = hashlib.sha256(secret.encode()).hexdigest()
    new_secret, new_hash = new_refresh_token()
    result = await db.execute(
        update(AuthSession)
        .where(
            AuthSession.id == session_id,
            AuthSession.refresh_hash == presented_hash,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > utc_now(),
        )
        .values(refresh_hash=new_hash)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise unauthorized()
    audit(db, "auth.refresh", actor_id=admin.id, target_id=admin.id)
    await db.commit()
    data = TokenOut(
        access_token=make_access_token(admin, session_id),
        refresh_token=f"{session_id}.{new_secret}",
        expires_in=ACCESS_TOKEN_SECONDS,
    )
    return api_response(data=data.model_dump())


@router.post("/logout", summary="退出登录")
async def logout(request: Request, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """撤销当前访问令牌所属的登录会话。"""
    await db.execute(
        update(AuthSession)
        .where(
            AuthSession.id == request.state.auth_session_id,
            AuthSession.admin_id == admin.id,
        )
        .values(revoked_at=utc_now())
    )
    audit(db, "auth.logout", actor_id=admin.id, target_id=admin.id)
    await db.commit()
    return api_response(message="退出成功")


@router.get("/me", summary="获取当前用户信息")
async def me(admin: Admin = Depends(get_current_admin)):
    """返回当前登录用户的非敏感资料。"""
    return api_response(data=UserOut.model_validate(admin).model_dump(mode="json"), datetime_format=False)


@router.put("/me", summary="修改当前用户资料")
async def update_me(body: ProfileUpdateIn, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """更新当前用户昵称或邮箱并记录审计事件。"""
    fields = body.model_fields_set
    if "mail" in fields:
        mail = str(body.mail).lower() if body.mail else None
        await ensure_unique_identity(db, mail=mail, exclude_id=admin.id)
        admin.mail = mail
    if "nickname" in fields:
        admin.nickname = body.nickname
    audit(db, "user.profile_updated", actor_id=admin.id, target_id=admin.id)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10003, detail="用户名或邮箱已被使用") from None
    await db.refresh(admin)
    return api_response(data=UserOut.model_validate(admin).model_dump(mode="json"), datetime_format=False)


@router.post("/password", summary="修改当前用户密码")
async def change_password(body: PasswordChangeIn, admin: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """核对旧密码，更新密码并撤销该用户的全部会话。"""
    if not check_user_password(admin, body.current_password):
        raise CustomException(status_code=400, custom_code=10005, detail="当前密码错误")
    set_user_password(admin, body.new_password)
    await revoke_sessions(db, admin)
    audit(db, "auth.password_changed", actor_id=admin.id, target_id=admin.id)
    await db.commit()
    return api_response(message="密码修改成功")
