# -*- coding: utf-8 -*-
# @Time    : 2026-11-23
# @Author  : Codex
# @File    : test_mcp_results_migration.py

"""隔离验证 066 的历史空值、结构一致、往返和有损回退保护。"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_budget_results_migration_roundtrip(tmp_path):
    """只操作临时 SQLite，已有结果来源不得被静默丢弃。"""
    database = tmp_path / "066.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*args, success=True):
        """运行真实 Alembic 命令，不连接业务库。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True, timeout=60
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "065")
    migrate("upgrade", "066")
    migrate("check")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_artifact (id,owner_id,filename,media_type,byte_size,sha256,storage_key,source,state,"
            "create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES ('old',1,'old.json','application/json',2,'hash','key','generated','ready','2026-11-23','2026-11-23',1,1)"
        )
        assert db.execute("SELECT tool_result FROM yp_artifact").fetchone() == (None,)
    migrate("downgrade", "065")
    migrate("upgrade", "066")
    with sqlite3.connect(database) as db:
        db.execute('UPDATE yp_artifact SET tool_result = \'{"scope":"single_response_redacted"}\'')
    result = migrate("downgrade", "065", success=False)
    assert "MCP 结果来源" in result.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("066",)
        db.execute("UPDATE yp_artifact SET tool_result=NULL")
    migrate("check")
