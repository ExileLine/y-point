# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 09:16:54
# @Author  : yangyuexiong
# @File    : test_worker_artifact_restart.py

"""验证独立 worker 重启后的回答产物补发与去重。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import Base
from app.services.message_attachments import load_message_artifacts
from app.services.run_manager import claim_run, complete_run, create_run, get_owned_messages, replay_events


def test_worker_restart_publishes_pending_answer_once(tmp_path: Path) -> None:
    """验证两次独立 worker 启动仅补发一次待发布回答产物。"""
    database = tmp_path / "worker-restart.db"
    checkpoint = tmp_path / "checkpoint.db"
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(mode=0o700)

    async def seed() -> str:
        """准备隔离测试数据：验证两次独立 worker 启动仅补发一次待发布回答产物。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="worker-restart", password="unused", role="user", status=1, is_deleted=0))
                run = await create_run(db, 1, input_text="导出回答", publish_answer_artifact=True)
                claimed = await claim_run(db, run.id)
                await complete_run(db, run.id, claimed.lease_token, final_text="延迟发布的回答")
                return run.id
        finally:
            await engine.dispose()

    run_id = asyncio.run(seed())
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
            "ARTIFACT_STORAGE_DIR": str(artifact_root),
            "CHECKPOINT_BACKEND": "sqlite",
        }
    )
    root = Path(__file__).resolve().parents[1]
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "app.cli.run_worker", "--once", "--checkpoint-db", str(checkpoint)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr

    async def verify() -> None:
        """核对持久化结果：验证两次独立 worker 启动仅补发一次待发布回答产物。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                artifacts = (await db.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
                assert len(artifacts) == 1 and artifacts[0].state == "ready"
                assert (artifact_root / artifacts[0].storage_key).read_text() == "延迟发布的回答"
                events = await replay_events(db, run_id, 1)
                assert [item.artifact_id for item in events if item.event_type == "artifact_published"] == [artifacts[0].id]
                messages = await get_owned_messages(db, run_id, 1)
                rendered = await load_message_artifacts(db, 1, [messages[-1]])
                assert rendered[messages[-1].id][0]["artifact_id"] == artifacts[0].id
        finally:
            await engine.dispose()

    asyncio.run(verify())
