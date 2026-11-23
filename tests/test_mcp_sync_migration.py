# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 15:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_sync_migration.py

"""验证 063 历史连接兼容、结构一致性、回执保存与安全回退。"""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_mcp_sync_migration_roundtrip_and_auth_downgrade_guard(tmp_path: Path) -> None:
    """在隔离业务库往返 062/063，新鉴权无法静默回退。"""
    database = tmp_path / "sync-migration.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*args, success=True):
        """运行真实 Alembic，不依赖用户本地业务库。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args], env=environment, capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1], timeout=60
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        return result

    migrate("upgrade", "062")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_mcp_connection (id,owner_id,name,version,config,enabled,share_credentials,approval_mode,credentials_revoked_through,"
            "create_time,update_time,create_timestamp,update_timestamp) VALUES (1,1,'legacy',1,?,1,0,'writes',0,'2026-11-23','2026-11-23',1,1)",
            (
                json.dumps(
                    {
                        "transport": "streamable_http",
                        "url": "https://legacy.invalid/mcp",
                    }
                ),
            ),
        )
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT config_revoked_through FROM yp_mcp_connection").fetchone() == (0,)
        original = db.execute("SELECT config FROM yp_mcp_connection").fetchone()[0]
        config = {
            **json.loads(original),
            "auth_type": "api_key",
            "api_key_header": "X-Key",
        }
        db.execute("UPDATE yp_mcp_connection SET config = ?", (json.dumps(config),))
    denied = migrate("downgrade", "062", success=False)
    assert "仍有新 MCP 鉴权" in denied.stderr
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "063"
        db.execute("UPDATE yp_mcp_connection SET config = ?, config_revoked_through = 1", (original,))
    migrate("downgrade", "062", success=False)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE yp_mcp_connection SET config_revoked_through = 0")
    migrate("downgrade", "062")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT config FROM yp_mcp_connection").fetchone()[0] == original
        assert "config_revoked_through" not in {row[1] for row in db.execute("PRAGMA table_info(yp_mcp_connection)")}
    migrate("upgrade", "head")
    migrate("check")
