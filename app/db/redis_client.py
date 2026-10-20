# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : redis_client.py

from typing import Optional

from loguru import logger
from redis.asyncio import Redis

from app.core.config import get_config

project_config = get_config()
REDIS_URL = project_config.redis_url

redis_pool: Optional[Redis] = None


async def create_redis_connection_pool(force: bool = False) -> Redis:
    global redis_pool
    if redis_pool and not force:
        return redis_pool
    if redis_pool and force:
        await close_redis_connection_pool()
    redis_pool = Redis.from_url(REDIS_URL)
    logger.info("Redis 连接池已创建")
    return redis_pool


async def close_redis_connection_pool():
    global redis_pool
    if redis_pool:
        close_method = getattr(redis_pool, "aclose", None)
        if close_method:
            await close_method()
        else:
            await redis_pool.close()
        redis_pool = None
        logger.info("Redis 连接池已关闭")


async def get_redis_pool() -> Redis:
    if not redis_pool:
        raise RuntimeError("Redis 连接池未初始化，请先调用 create_redis_connection_pool()")
    return redis_pool


async def set_key_value(key, value, ex=None):
    pool = await get_redis_pool()
    await pool.set(key, value, ex)


async def get_value(key):
    pool = await get_redis_pool()
    value = await pool.get(key)
    return value


async def delete_value(key):
    pool = await get_redis_pool()
    await pool.delete(key)


async def redis_one_get(k):
    redis = Redis.from_url(REDIS_URL)
    res = await redis.get(k)
    await redis.close()
    return res


async def redis_one_set(k, v):
    redis = Redis.from_url(REDIS_URL)
    await redis.set(k, v)
    await redis.close()