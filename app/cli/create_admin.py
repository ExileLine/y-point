# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : create_admin.py

import argparse
import asyncio
from getpass import getpass

from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal, close_db
from app.models.admin import Admin
from app.services.identity import audit, ensure_unique_identity, set_user_password


async def create_first_admin(username: str, mail: str | None, password: str) -> None:
    """仅在没有有效管理员时创建首位管理员。"""
    async with AsyncSessionLocal() as db:
        if await db.scalar(select(func.count()).select_from(Admin).where(Admin.role == "admin", Admin.status == 1, Admin.is_deleted == 0)):
            raise SystemExit("已有有效管理员；请通过管理员 API 创建后续账户")
        await ensure_unique_identity(db, username=username, mail=mail)
        admin = Admin(username=username, mail=mail, role="admin", status=1, is_deleted=0)
        set_user_password(admin, password)
        db.add(admin)
        await db.flush()
        audit(db, "user.bootstrap_admin", actor_id=admin.id, target_id=admin.id)
        await db.commit()
        print(f"管理员已创建：{admin.id} ({admin.username})")


def main() -> None:
    """读取命令行参数和交互式密码并执行管理员初始化。"""
    parser = argparse.ArgumentParser(description="初始化首位管理员")
    parser.add_argument("username")
    parser.add_argument("--mail")
    args = parser.parse_args()
    username = args.username.strip().lower()
    mail = args.mail.strip().lower() if args.mail else None
    if (
        not 3 <= len(username) <= 64
        or not username[0].isascii()
        or not username[0].isalpha()
        or not all(char.isascii() and (char.isalnum() or char == "_") for char in username)
    ):
        parser.error("用户名需以字母开头，使用 3-64 位英文字母、数字或下划线")
    password = getpass("密码：")
    confirmation = getpass("确认密码：")
    if password != confirmation:
        parser.error("两次密码不一致")

    async def run() -> None:
        """在同一个事件循环中完成创建及数据库连接关闭。"""
        try:
            await create_first_admin(username, mail, password)
        finally:
            await close_db()

    asyncio.run(run())


if __name__ == "__main__":
    main()
