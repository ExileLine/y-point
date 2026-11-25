# -*- coding: utf-8 -*-
# @Time    : 2026-11-24 23:27:00
# @Author  : yangyuexiong
# @File    : test_ag_ui_history_migration.py

"""067 仅新增可重建缓存，升级与降级都保留原会话数据。"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_public_history_migration_roundtrip(tmp_path):
    """验证缓存迁移可往返执行，且始终保留原会话数据。"""
    database = tmp_path / "067.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*args):
        """在临时数据库执行 Alembic 命令，并验证退出状态。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "066")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_agent_session (id,owner_id,agent_id,thread_id,title,archived,version,create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES ('historical',1,1,'thread','原有会话',0,12,'2026-11-24','2026-11-24',1,1)"
        )
        columns = ",".join(f'"{row[1]}"' for row in db.execute("PRAGMA table_info(yp_agent_session)"))
        before = db.execute(f"SELECT {columns} FROM yp_agent_session").fetchall()
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        assert db.execute(f"SELECT {columns} FROM yp_agent_session").fetchall() == before
        assert db.execute("SELECT COUNT(*) FROM yp_agent_run_public_history").fetchone() == (0,)
        db.execute("INSERT INTO yp_agent_run_public_history VALUES ('derived',4,1,'[]')")
    migrate("downgrade", "066")
    with sqlite3.connect(database) as db:
        assert db.execute(f"SELECT {columns} FROM yp_agent_session").fetchall() == before
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='yp_agent_run_public_history'").fetchall()
    migrate("upgrade", "head")
    migrate("check")
