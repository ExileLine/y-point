# -*- coding: utf-8 -*-
# @Time    : 2026-11-25 09:02:32
# @Author  : yangyuexiong
# @File    : test_mcp_session_approval_migration.py

"""验证 068 升降级保留历史会话、审批和其原有决定。"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_session_approval_migration_preserves_old_rows(tmp_path):
    """在临时 SQLite 往返迁移，旧数据默认逐次询问且可重新升级。"""
    database = tmp_path / "068.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*arguments):
        """执行真实 Alembic 命令，失败时输出迁移诊断。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "067")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_agent_session (id, owner_id, agent_id, thread_id, title, archived, version, "
            "create_time, update_time, create_timestamp, update_timestamp) "
            "VALUES ('session', 1, 1, 'thread', '原有会话', 0, 12, '2026-11-25', '2026-11-25', 1, 1)"
        )
        db.execute(
            "INSERT INTO yp_run_approval (id, run_id, owner_id, checkpoint_id, actions, state, decision, decisions, "
            "expires_at, create_time, update_time, create_timestamp, update_timestamp) "
            "VALUES ('approval', 'run', 1, 'checkpoint', '[]', 'approved', 'approve', '[]', '2026-11-26', '2026-11-25', '2026-11-25', 1, 1)"
        )
        originals = {}
        for table in ("yp_agent_session", "yp_run_approval"):
            columns = ",".join(f'"{row[1]}"' for row in db.execute(f"PRAGMA table_info({table})"))
            originals[table] = (columns, db.execute(f"SELECT {columns} FROM {table}").fetchall())
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT approval_policy, approval_policy_version FROM yp_agent_session").fetchone() == (None, 1)
        assert db.execute("SELECT scope FROM yp_run_approval").fetchone() == ("once",)
        for table, (columns, rows) in originals.items():
            assert db.execute(f"SELECT {columns} FROM {table}").fetchall() == rows
    migrate("downgrade", "067")
    with sqlite3.connect(database) as db:
        for table, (columns, rows) in originals.items():
            assert db.execute(f"SELECT {columns} FROM {table}").fetchall() == rows
    migrate("upgrade", "head")
    migrate("check")
