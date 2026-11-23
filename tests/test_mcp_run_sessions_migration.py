# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_run_sessions_migration.py

"""在临时库验证 065 升降级、旧审批保留和新语义回退保护。"""

import os
import asyncio
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_run_session_approval_migration_roundtrip(tmp_path):
    """旧 reject 不改写；已有新版审批或会话归属时拒绝有损回退。"""
    database = tmp_path / "065.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*arguments, success=True):
        """只迁移临时测试库。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=60
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "064")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_run_approval (id,run_id,owner_id,checkpoint_id,actions,state,expires_at,create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES ('legacy','run',1,'checkpoint','[]','rejected','2026-11-23','2026-11-23','2026-11-23',1,1)"
        )
    migrate("upgrade", "065")
    migrate("upgrade", "head")
    migrate("check")
    migrate("downgrade", "065")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT state,decision,decisions FROM yp_run_approval").fetchone() == ("rejected", None, None)
        db.execute("UPDATE yp_run_approval SET state='resolved',decision='reject_continue',decisions='[\"reject\"]'")
    assert "新版逐项审批" in migrate("downgrade", "064", success=False).stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("065",)
        db.execute("UPDATE yp_run_approval SET state='rejected',decision=NULL,decisions=NULL")

    async def add_owner():
        """用正式模型写入归属审计，验证回退不能丢失它。"""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.services.run_manager import create_run

        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with async_sessionmaker(engine).begin() as db:
                run = await create_run(db, 1, input_text="有状态会话")
                run.mcp_worker_id = "worker-a"
        finally:
            await engine.dispose()

    migrate("upgrade", "head")
    asyncio.run(add_owner())
    migrate("downgrade", "065")
    assert "MCP 会话归属" in migrate("downgrade", "064", success=False).stderr
    with sqlite3.connect(database) as db:
        db.execute("UPDATE yp_agent_run SET mcp_worker_id=NULL")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_run_mcp_resource (id,run_id,encrypted_payload,retired,expires_at,next_attempt_at,"
            "create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES ('pending','run','encrypted-test',1,'2026-11-23','2026-11-23','2026-11-23','2026-11-23',1,1)"
        )
    assert "远端会话待回收" in migrate("downgrade", "064", success=False).stderr
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM yp_run_mcp_resource")
    migrate("downgrade", "064")
    migrate("upgrade", "head")
    migrate("check")
