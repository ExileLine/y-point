# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:34:17
# @Author  : yangyuexiong
# @File    : test_mcp_connection_migration.py

"""验证 058 的升级、结构一致性、空使用回退和已引用版本保护。"""

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.mcp_connection import McpConnection
from app.models.tool import UserToolVersion


def test_mcp_connection_migration_roundtrip(tmp_path: Path) -> None:
    """隔离 SQLite 完成升级、回退和重升；已引用的连接快照不能被静默丢弃。"""
    path = tmp_path / "migration.db"
    env = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(path),
    }

    def migrate(*args, success=True):
        """运行真实 Alembic，失败时仅报告受控测试输出。"""
        result = subprocess.run([sys.executable, "-m", "alembic", *args], env=env, text=True, capture_output=True, timeout=90)
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "head")
    migrate("check")
    migrate("downgrade", "057")
    with sqlite3.connect(path) as db:
        assert db.execute("select count(*) from sqlite_master where name='yp_mcp_connection'").fetchone()[0] == 0
        assert "mcp_config" not in {row[1] for row in db.execute("pragma table_info(yp_user_tool_version)")}
    migrate("upgrade", "head")

    async def add_reference():
        """写入最小执行快照，验证降级不会悄悄丢失传输或固定版本。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        try:
            async with async_sessionmaker(engine).begin() as db:
                db.add(
                    McpConnection(
                        id=1,
                        owner_id=1,
                        name="test",
                        config={
                            "transport": "stdio",
                        },
                    )
                )
                db.add(
                    UserToolVersion(
                        tool_id=1,
                        version=1,
                        content_hash="a" * 64,
                        input_schema={
                            "type": "object",
                        },
                        effect="read",
                        requires_approval=False,
                        mcp_connection_id=1,
                        mcp_connection_version=1,
                        mcp_config={
                            "transport": "stdio",
                        },
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(add_reference())
    result = migrate("downgrade", "057", success=False)
    assert "已有工具引用 MCP 连接" in result.stderr
    with sqlite3.connect(path) as db:
        assert db.execute("select version_num from alembic_version").fetchone()[0] == "058"
        assert db.execute("select mcp_connection_id from yp_user_tool_version").fetchone()[0] == 1
        assert db.execute("pragma quick_check").fetchone()[0] == "ok"
