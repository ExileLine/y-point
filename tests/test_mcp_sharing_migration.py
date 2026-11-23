# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 18:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_sharing_migration.py

"""实际验证 064 隔离迁移、旧关系保留与授权来源回退保护。"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_sharing_migration_roundtrip_and_downgrade_guards(tmp_path: Path) -> None:
    """空表可往返，有共享、Agent 选择或 Run 来源时拒绝静默丢弃。"""
    database = tmp_path / "sharing-migration.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*args, success=True):
        """只迁移临时业务库，绝不使用项目实际数据库。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=environment, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=60
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "063")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_agent_tool (agent_id,tool_id,tool_version,create_time,update_time,create_timestamp,update_timestamp,is_deleted,status) "
            "VALUES (1,7,2,'2026-11-23','2026-11-23',1,1,0,1)"
        )
        db.execute(
            "INSERT INTO yp_run_tool (run_id,tool_id,tool_version,content_hash,create_time,update_time,create_timestamp,update_timestamp,is_deleted,status) "
            "VALUES ('legacy',7,2,'legacy-hash','2026-11-23','2026-11-23',1,1,0,1)"
        )
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT tool_id,tool_version FROM yp_agent_tool").fetchall() == [(7, 2)]
        assert db.execute("SELECT content_hash,mcp_connection_id FROM yp_run_tool").fetchone() == ("legacy-hash", None)
        db.execute(
            "INSERT INTO yp_mcp_connection_share (connection_id,user_id,version,tool_ids,allow_credentials,"
            "create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES (1,2,1,'[7]',0,'2026-11-23','2026-11-23',1,1)"
        )
    assert "仍有 MCP 连接共享" in migrate("downgrade", "063", success=False).stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("064",)
        db.execute("DELETE FROM yp_mcp_connection_share")
        db.execute(
            "INSERT INTO yp_agent_mcp_tool (agent_id,connection_id,tool_id,tool_version,create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES (1,1,7,2,'2026-11-23','2026-11-23',1,1)"
        )
    migrate("downgrade", "063", success=False)
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM yp_agent_mcp_tool")
        db.execute("UPDATE yp_run_tool SET mcp_connection_id=1")
    assert "历史 Run 快照" in migrate("downgrade", "063", success=False).stderr
    with sqlite3.connect(database) as db:
        db.execute("UPDATE yp_run_tool SET mcp_connection_id=NULL")
    migrate("downgrade", "063")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT tool_id,tool_version FROM yp_agent_tool").fetchall() == [(7, 2)]
    migrate("upgrade", "head")
    migrate("check")
