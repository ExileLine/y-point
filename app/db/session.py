# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : session.py

from collections.abc import AsyncGenerator

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.models  # noqa: F401 - 注册所有业务模型以核对运行时表结构。
from app.core.config import get_config
from app.models.base import Base

project_config = get_config()

engine = create_async_engine(project_config.database_url, pool_pre_ping=True, pool_recycle=3600, echo=project_config.DEBUG)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """提供请求级异步数据库会话并在结束时关闭。"""
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    """创建 SQLite 所需目录并验证数据库连接与映射结构。"""
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT 1"))
        await conn.run_sync(check_mapped_schema)
    logger.info(f">>> {result.scalar()}")
    logger.info(">>> 数据库连接池初始化完成")


def check_mapped_schema(connection) -> None:
    """在启动时发现业务模型需要但数据库尚未迁移的表和列。"""
    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            raise RuntimeError(f"数据库缺少表 {table.name}；请先执行 uv run alembic upgrade head")
        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns = {column.name for column in table.columns} - existing_columns
        if missing_columns:
            name = sorted(missing_columns)[0]
            raise RuntimeError(f"数据库缺少列 {table.name}.{name}；请先执行 uv run alembic upgrade head")


async def close_db():
    """释放异步数据库引擎的连接池。"""
    await engine.dispose()
    logger.info(">>> 数据库连接池已关闭")
