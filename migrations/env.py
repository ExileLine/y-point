# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 08:51:32
# @Author  : yangyuexiong
# @File    : env.py

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_config
import app.models  # noqa: F401 - registers the models with Base.metadata
from app.models.base import Base


config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """按配置生成离线迁移 SQL，不建立数据库连接。"""
    context.configure(
        url=get_config().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={
            "paramstyle": "named",
        },
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection) -> None:
    """使用已提供的数据库连接在事务中执行迁移。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """创建异步连接执行迁移，并在结束后释放连接池。"""
    engine = create_async_engine(get_config().database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(run_sync_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
