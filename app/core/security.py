# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : security.py

import json
import secrets

from fastapi import Depends, Header
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db.redis_client as rp
from app.core.custom_exception import CustomException
from app.db.redis_client import get_value, set_key_value
from app.db.session import get_db_session
from app.models.admin import Admin


class Token:
    def __init__(self):
        self.token = None
        self.timeout = 3600 * 24 * 30

    async def gen_token(self):
        token = secrets.token_urlsafe(32)
        self.token = token
        return token

    @staticmethod
    async def get_user_info(token: str):
        query_user_info = await get_value(token)
        if not query_user_info:
            return False
        user_info = json.loads(query_user_info)
        return user_info

    async def single_login(self, key: str, user_info_json_str: str):
        old_key_list = await rp.redis_pool.keys(pattern=f"{key}*")
        old_token_list = [await get_value(old_key) for old_key in old_key_list]
        if old_key_list or old_token_list:
            logger.debug(f"single_login revoke tokens: keys={len(old_key_list)} tokens={len(old_token_list)}")
        if old_key_list:
            await rp.redis_pool.delete(*old_key_list)
        valid_old_tokens = [token for token in old_token_list if token]
        if valid_old_tokens:
            await rp.redis_pool.delete(*valid_old_tokens)

        await self.gen_token()
        await set_key_value(f"{key}{self.token}", self.token, self.timeout)
        await set_key_value(self.token, user_info_json_str, self.timeout)

    async def many_login(self, key: str, user_info_json_str: str):
        await self.gen_token()
        await set_key_value(f"{key}{self.token}", self.token, self.timeout)
        await set_key_value(self.token, user_info_json_str, self.timeout)


async def get_token_header(token: str = Header()):
    query_user_info = await get_value(token)
    if not query_user_info:
        raise CustomException(detail="未授权", custom_code=401)
    user_info = json.loads(query_user_info)
    return user_info


async def check_admin_existence(
    user_info: dict = Depends(get_token_header),
    db: AsyncSession = Depends(get_db_session),
):
    try:
        admin_id = int(user_info.get("id"))
    except (TypeError, ValueError):
        raise CustomException(detail="无效的用户身份", custom_code=401)
    stmt = select(Admin).where(Admin.id == admin_id)
    admin = (await db.execute(stmt)).scalars().first()
    if not admin:
        raise CustomException(detail=f"后台用户 {admin_id} 不存在", custom_code=10002)
    return admin