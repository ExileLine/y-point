# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : users.py

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import require_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.audit_event import AuditEvent
from app.schemas.auth import AuditOut, PasswordResetIn, UserCreateIn, UserOut, UserPage, UserUpdateIn
from app.services.identity import audit, ensure_unique_identity, revoke_sessions, set_user_password


router = APIRouter(prefix="/users", tags=["users"])


async def get_user_or_404(db: AsyncSession, user_id: int) -> Admin:
    """读取未删除用户；不存在时抛出业务异常。"""
    user = await db.get(Admin, user_id)
    if user is None or user.is_deleted != 0:
        raise CustomException(status_code=404, custom_code=10002, detail="用户不存在")
    return user


@router.get("/audit/events", summary="查询用户操作审计")
async def list_audit_events(
    limit: int = Query(50, ge=1, le=200),
    actor: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按时间倒序返回管理员可见的审计记录。"""
    events = (await db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit))).all()
    data = [AuditOut.model_validate(event).model_dump(mode="json") for event in events]
    return api_response(data=data, is_pop=False)


async def commit_or_conflict(db: AsyncSession) -> None:
    """提交用户修改，将唯一索引冲突转换为业务异常。"""
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10003, detail="用户名或邮箱已被使用") from None


@router.post("", summary="创建用户")
async def create_user(body: UserCreateIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """由管理员创建用户并记录创建审计。"""
    username = body.username.lower()
    mail = str(body.mail).lower() if body.mail else None
    await ensure_unique_identity(db, username=username, mail=mail)
    user = Admin(
        username=username,
        mail=mail,
        nickname=body.nickname,
        role=body.role,
        status=1,
        is_deleted=0,
        creator=actor.username,
        creator_id=actor.id,
    )
    set_user_password(user, body.password)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10003, detail="用户名或邮箱已被使用") from None
    audit(db, "user.created", actor_id=actor.id, target_id=user.id)
    await commit_or_conflict(db)
    await db.refresh(user)
    return api_response(http_code=201, code=201, data=UserOut.model_validate(user).model_dump(mode="json"), datetime_format=False)


@router.get("", summary="分页查询用户")
async def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    actor: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按 ID 分页列出未删除用户。"""
    query = select(Admin).where(Admin.is_deleted == 0)
    total = await db.scalar(select(func.count()).select_from(Admin).where(Admin.is_deleted == 0))
    users = (await db.scalars(query.order_by(Admin.id).offset((page - 1) * page_size).limit(page_size))).all()
    data = UserPage(items=[UserOut.model_validate(user) for user in users], total=total or 0, page=page, page_size=page_size)
    return api_response(data=data.model_dump(mode="json"), datetime_format=False)


@router.get("/{user_id}", summary="查询用户详情")
async def get_user(user_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """返回指定用户的非敏感资料。"""
    user = await get_user_or_404(db, user_id)
    return api_response(data=UserOut.model_validate(user).model_dump(mode="json"), datetime_format=False)


@router.put("/{user_id}", summary="修改用户资料及状态")
async def update_user(user_id: int, body: UserUpdateIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """更新用户资料、角色或状态，并撤销受影响的会话。"""
    user = await get_user_or_404(db, user_id)
    fields = body.model_fields_set
    if "mail" in fields:
        mail = str(body.mail).lower() if body.mail else None
        await ensure_unique_identity(db, mail=mail, exclude_id=user.id)
        user.mail = mail
    if "nickname" in fields:
        user.nickname = body.nickname
    if "role" in fields or "status" in fields:
        new_role = body.role if "role" in fields else user.role
        new_status = body.status if "status" in fields else user.status
        if new_role is None or new_status is None:
            raise CustomException(status_code=400, custom_code=10005, detail="角色和状态不能为空")
        if user.role == "admin" and user.status == 1 and (new_role != "admin" or new_status != 1):
            active_admins = await db.scalar(
                select(func.count()).select_from(Admin).where(Admin.role == "admin", Admin.status == 1, Admin.is_deleted == 0)
            )
            if active_admins <= 1:
                raise CustomException(status_code=409, custom_code=10005, detail="不能停用或降级最后一位管理员")
        if new_role != user.role or new_status != user.status:
            user.role = new_role
            user.status = new_status
            await revoke_sessions(db, user)
    user.modifier = actor.username
    user.modifier_id = actor.id
    audit(db, "user.updated", actor_id=actor.id, target_id=user.id)
    await commit_or_conflict(db)
    await db.refresh(user)
    return api_response(data=UserOut.model_validate(user).model_dump(mode="json"), datetime_format=False)


@router.post("/{user_id}/reset-password", summary="重置用户密码")
async def reset_password(
    user_id: int,
    body: PasswordResetIn,
    actor: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """由管理员重置目标用户密码并撤销其全部会话。"""
    user = await get_user_or_404(db, user_id)
    set_user_password(user, body.new_password)
    await revoke_sessions(db, user)
    audit(db, "auth.password_reset", actor_id=actor.id, target_id=user.id)
    await db.commit()
    return api_response(message="密码重置成功")
