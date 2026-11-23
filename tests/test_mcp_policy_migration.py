# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 10:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_policy_migration.py

"""验证 MCP 策略迁移保留旧审批、执行摘要及活动运行回退保护。"""

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.run_manager import create_run


def test_mcp_policy_migration_preserves_legacy_and_roundtrip(tmp_path: Path) -> None:
    """隔离库从 061 升级、检查、受保护回退及重升，不使用本机业务数据。"""
    database = tmp_path / "mcp-policy-migration.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*arguments: str, success: bool = True):
        """执行真实 Alembic，保留失败输出以验证回退保护。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert (result.returncode == 0) is success, result.stdout + result.stderr
        return result

    migrate("upgrade", "061")
    with sqlite3.connect(database) as db:
        for tool_id, required in [(1, True), (2, False)]:
            db.execute(
                "INSERT INTO yp_user_tool_version "
                "(tool_id,version,content_hash,input_schema,effect,recovery_policy,requires_approval,verified,share_credentials,"
                "requirements,environment_state,environment_attempts,write_contract_verified,create_time,update_time,create_timestamp,update_timestamp) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tool_id,
                    1,
                    "a" * 64,
                    '{"type":"object"}',
                    "write",
                    "manual",
                    required,
                    True,
                    False,
                    "[]",
                    "not_required",
                    0,
                    False,
                    "2026-11-23 00:00:00",
                    "2026-11-23 00:00:00",
                    1795363200,
                    1795363200,
                ),
            )
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT approval_mode,requires_approval,content_hash,effect_source FROM yp_user_tool_version ORDER BY tool_id").fetchall()
        assert rows == [("always", 1, "a" * 64, None), ("inherit", 0, "a" * 64, None)]

    async def add_active() -> str:
        """写入需要本轮收紧的新运行，验证回退不会静默丢失要求。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with async_sessionmaker(engine).begin() as db:
                run = await create_run(db, 1, input_text="收紧审批", require_mcp_approval=True)
                return run.id
        finally:
            await engine.dispose()

    run_id = asyncio.run(add_active())
    denied = migrate("downgrade", "061", success=False)
    assert "仍有使用新 MCP 审批策略的活动运行" in denied.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "062"
        db.execute("UPDATE yp_agent_run SET state = 'completed' WHERE id = ?", (run_id,))
    migrate("downgrade", "061")
    with sqlite3.connect(database) as db:
        assert "approval_mode" not in {row[1] for row in db.execute("PRAGMA table_info(yp_user_tool_version)")}
        assert db.execute("SELECT requires_approval,content_hash FROM yp_user_tool_version ORDER BY tool_id").fetchall() == [(1, "a" * 64), (0, "a" * 64)]
    migrate("upgrade", "head")
    migrate("check")
