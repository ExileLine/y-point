# -*- coding: utf-8 -*-
# @Time    : 2026-11-27 13:49:41
# @Author  : yangyuexiong
# @File    : test_approval_deadline_migration.py

"""验证无限等待审批的结构升级、历史状态保护及回退约束。"""

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from datetime import timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import ApprovalDecision
from app.models.base import shanghai_now
from app.services.run_manager import claim_run, create_approval_request, create_run, decide_approval, expire_approvals


def test_approval_deadline_migration_preserves_history(tmp_path: Path) -> None:
    """068 升级清除活动审批期限，保留历史终态，且无期限审批必须处理后才能回退。"""
    database = tmp_path / "approval-deadline.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(*arguments: str, succeeds: bool = True) -> None:
        """仅在临时数据库运行真实 Alembic 命令，核对预期成功或保护性拒绝。"""
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if succeeds:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0 and "仍有无截止时间的待审批请求" in result.stdout + result.stderr

    async def seed() -> None:
        """用真实状态服务生成待审批、已批准与已过期的旧版本记录。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            for label in ("pending", "approved", "expired"):
                async with sessions.begin() as db:
                    run = await create_run(db, 1, input_text=label)
                    token = (await claim_run(db, run.id)).lease_token
                    approval = await create_approval_request(
                        db,
                        run.id,
                        token,
                        label,
                        [
                            {
                                "name": "inspect",
                                "arguments_hash": "a" * 64,
                            },
                        ],
                        ttl_seconds=60,
                    )
                    if label == "approved":
                        await decide_approval(db, run.id, 1, ApprovalDecision.APPROVE)
                    elif label == "expired":
                        approval.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
                        await db.flush()
                        assert await expire_approvals(db) == 1
        finally:
            await engine.dispose()

    migrate("upgrade", "068")
    asyncio.run(seed())
    with sqlite3.connect(database) as db:
        history = db.execute("SELECT id, state, expires_at, decision FROM yp_run_approval WHERE state != 'pending' ORDER BY id").fetchall()
        runs = db.execute("SELECT id, state, failure_code, version, event_seq FROM yp_agent_run ORDER BY id").fetchall()
        event_count = db.execute("SELECT count(*) FROM yp_agent_run_event").fetchone()
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT expires_at FROM yp_run_approval WHERE state = 'pending'").fetchone() == (None,)
        assert db.execute("SELECT id, state, expires_at, decision FROM yp_run_approval WHERE state != 'pending' ORDER BY id").fetchall() == history
        assert db.execute("SELECT id, state, failure_code, version, event_seq FROM yp_agent_run ORDER BY id").fetchall() == runs
        assert db.execute("SELECT count(*) FROM yp_agent_run_event").fetchone() == event_count
    migrate("downgrade", "068", succeeds=False)
    with sqlite3.connect(database) as db:
        db.execute("UPDATE yp_run_approval SET state = 'cancelled' WHERE state = 'pending'")
        db.execute("UPDATE yp_agent_run SET state = 'cancelled' WHERE state = 'waiting_for_approval'")
    migrate("downgrade", "068")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT count(*) FROM yp_run_approval WHERE expires_at IS NULL").fetchone() == (0,)
        restored = db.execute("SELECT id, state, expires_at, decision FROM yp_run_approval WHERE state IN ('approved', 'expired') ORDER BY id").fetchall()
        assert restored == history
    migrate("upgrade", "head")
    migrate("check")
