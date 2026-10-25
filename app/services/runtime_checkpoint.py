# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:43:29
# @Author  : yangyuexiong
# @File    : runtime_checkpoint.py

"""运行时的持久化检查点入口。"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import get_config


@contextmanager
def open_sqlite_checkpointer(database_path: Path) -> Iterator[SqliteSaver]:
    """打开独立的 SQLite 检查点文件，并在使用结束后关闭连接。"""
    if database_path == Path(":memory:"):
        raise ValueError("持久化检查点必须使用文件路径")
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with SqliteSaver.from_conn_string(str(database_path)) as checkpointer:
        yield checkpointer


@asynccontextmanager
async def open_async_sqlite_checkpointer(database_path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    """为异步运行时打开文件检查点，并在退出时关闭连接。"""
    if database_path == Path(":memory:"):
        raise ValueError("持久化检查点必须使用文件路径")
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with AsyncSqliteSaver.from_conn_string(str(database_path)) as checkpointer:
        yield checkpointer


@asynccontextmanager
async def open_async_checkpointer(database_path: Path) -> AsyncIterator[AsyncSqliteSaver | AsyncPostgresSaver]:
    """按部署配置打开检查点；业务库后端与检查点后端相互独立。"""
    config = get_config()
    backend = config.CHECKPOINT_BACKEND.lower()
    if backend == "sqlite":
        async with open_async_sqlite_checkpointer(database_path) as checkpointer:
            yield checkpointer
    elif backend == "postgres":
        if not config.CHECKPOINT_POSTGRES_DSN:
            raise ValueError("PostgreSQL 检查点必须配置独立连接地址")
        async with AsyncPostgresSaver.from_conn_string(config.CHECKPOINT_POSTGRES_DSN) as checkpointer:
            yield checkpointer
    else:
        raise ValueError("不支持的检查点存储后端")
