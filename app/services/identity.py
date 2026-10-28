# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : identity.py

import hashlib
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.password import hash_password, verify_password
from app.core.custom_exception import CustomException
from app.core.security import REFRESH_DAYS, new_refresh_token, utc_now, validate_password
from app.models.admin import Admin
from app.models.audit_event import AuditEvent
from app.models.auth_attempt import AuthAttempt
from app.models.auth_session import AuthSession


def audit(db: AsyncSession, action: str, actor_id: int | None = None, target_id: int | None = None, subject: str | None = None) -> None:
    """在当前事务中记录不包含明文凭据的审计事件。"""
    db.add(
        AuditEvent(
            action=action,
            actor_id=actor_id,
            target_id=target_id,
            subject_hash=hashlib.sha256(subject.encode()).hexdigest() if subject else None,
        )
    )


async def find_login_user(db: AsyncSession, identifier: str) -> Admin | None:
    """按用户名或邮箱查找未删除用户。"""
    value = identifier.strip().lower()
    return (
        await db.scalars(
            select(Admin).where(
                or_(func.lower(Admin.username) == value, func.lower(Admin.mail) == value),
                Admin.is_deleted == 0,
            )
        )
    ).first()


async def ensure_unique_identity(
    db: AsyncSession,
    *,
    username: str | None = None,
    mail: str | None = None,
    exclude_id: int | None = None,
) -> None:
    """检查用户名和邮箱未被其他有效用户使用。"""
    values = [value.lower() for value in (username, mail) if value]
    for value in values:
        query = select(Admin.id).where(
            or_(func.lower(Admin.username) == value, func.lower(Admin.mail) == value),
            Admin.is_deleted == 0,
        )
        if exclude_id is not None:
            query = query.where(Admin.id != exclude_id)
        if (await db.scalar(query)) is not None:
            raise CustomException(status_code=409, custom_code=10003, detail="用户名或邮箱已被使用")


async def check_login_limit(db: AsyncSession, identifier: str, client_ip: str) -> None:
    """检查账号及来源地址是否处于登录限制期。"""
    now = utc_now()
    for key in (f"account:{identifier.lower()}", f"ip:{client_ip}"):
        attempt = await db.get(AuthAttempt, hashlib.sha256(key.encode()).hexdigest())
        if attempt and attempt.locked_until and attempt.locked_until > now:
            raise CustomException(status_code=429, custom_code=429, detail="登录尝试过多，请稍后重试")


async def record_login_failure(db: AsyncSession, identifier: str, client_ip: str) -> None:
    """累计账号及来源地址的登录失败次数并记录审计事件。"""
    now = utc_now()
    table = AuthAttempt.__table__
    for key in (f"account:{identifier.lower()}", f"ip:{client_ip}"):
        attempt_id = hashlib.sha256(key.encode()).hexdigest()
        values = {"id": attempt_id, "failures": 0, "window_started_at": now}
        dialect = db.bind.dialect.name
        if dialect == "postgresql":
            statement = postgres_insert(table).values(**values).on_conflict_do_nothing(index_elements=[table.c.id])
        elif dialect == "mysql":
            statement = mysql_insert(table).values(**values).on_duplicate_key_update(failures=table.c.failures)
        else:
            statement = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=[table.c.id])
        await db.execute(statement)
        attempt = await db.get(AuthAttempt, attempt_id, with_for_update=True, populate_existing=True)
        if now - attempt.window_started_at >= timedelta(minutes=15):
            attempt.failures = 0
            attempt.window_started_at = now
            attempt.locked_until = None
        attempt.failures += 1
        if attempt.failures >= 5:
            attempt.locked_until = now + timedelta(minutes=15)
    audit(db, "auth.login_failed", subject=identifier.lower())
    await db.commit()


async def clear_login_limit(db: AsyncSession, identifier: str, client_ip: str) -> None:
    """登录成功后清除账号及来源地址的失败计数。"""
    ids = [hashlib.sha256(key.encode()).hexdigest() for key in (f"account:{identifier.lower()}", f"ip:{client_ip}")]
    await db.execute(update(AuthAttempt).where(AuthAttempt.id.in_(ids)).values(failures=0, locked_until=None))


async def create_session(db: AsyncSession, admin: Admin) -> tuple[AuthSession, str]:
    """在当前事务中创建会话并返回一次性可见的刷新令牌。"""
    token, token_hash = new_refresh_token()
    session = AuthSession(id=str(uuid4()), admin_id=admin.id, refresh_hash=token_hash, expires_at=utc_now() + timedelta(days=REFRESH_DAYS))
    db.add(session)
    return session, f"{session.id}.{token}"


async def revoke_sessions(db: AsyncSession, admin: Admin) -> None:
    """递增令牌版本并撤销用户的所有活动会话。"""
    admin.token_version += 1
    await db.execute(
        update(AuthSession)
        .where(
            AuthSession.admin_id == admin.id,
            AuthSession.revoked_at.is_(None),
        )
        .values(revoked_at=utc_now())
    )


def set_user_password(admin: Admin, password: str) -> None:
    """校验原始密码并保存其哈希值。"""
    validate_password(password)
    admin.password = hash_password(password)


def check_user_password(admin: Admin | None, password: str) -> bool:
    """核对用户密码；无用户或无效哈希均返回失败。"""
    if not admin or len(password.encode()) > 72:
        return False
    try:
        return verify_password(password, admin.password)
    except (TypeError, ValueError):
        return False
