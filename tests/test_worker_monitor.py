# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 10:04:10
# @Author  : yangyuexiong
# @File    : test_worker_monitor.py

"""验证执行器心跳、队列状态与健康接口。"""

import asyncio
import os
import sys
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.cli import run_worker as run_worker_module
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.base import Base, shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.run_worker_heartbeat import RunWorkerHeartbeat
from app.services.run_manager import claim_run, create_run, replay_events
from app.services.worker_monitor import inspect_worker_status, prune_worker_heartbeats, register_worker, stop_worker, touch_worker


def test_worker_does_not_recover_leases_when_checkpoint_store_is_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """检查点全局故障时，过期 Run 等恢复读取成功后再判定终态。"""

    class Checkpointer:
        async def setup(self) -> None:
            """模拟检查点初始化，避免测试依赖实际后端。"""
            pass

    @asynccontextmanager
    async def open_checkpointer(_path: Path):
        """提供监视器测试所需的受控检查点上下文。"""
        yield Checkpointer()

    recover = AsyncMock()
    monkeypatch.setattr(run_worker_module, "open_async_checkpointer", open_checkpointer)
    monkeypatch.setattr(run_worker_module, "reconcile_expired_runs", AsyncMock(side_effect=OSError("unavailable")))
    monkeypatch.setattr(run_worker_module, "recover_expired_runs", recover)
    with pytest.raises(OSError, match="unavailable"):
        asyncio.run(run_worker_module.run_worker(tmp_path / "unused.sqlite", once=True))
    recover.assert_not_awaited()


async def exercise_worker_status(database_path: Path) -> None:
    """验证新鲜心跳、失联心跳和运行积压的统计。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            await create_run(db, 1, input_text="queued")
            running = await create_run(db, 1, input_text="running")
            await claim_run(db, running.id)
            await db.execute(
                update(AgentRun).where(AgentRun.id == running.id).values(lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1))
            )
        async with sessions() as db:
            status = await inspect_worker_status(db)
            assert status["worker_available"] is False and status["queued_runs"] == 1
            assert status["oldest_queued_seconds"] is not None and status["expired_running_runs"] == 1
        async with sessions.begin() as db:
            await register_worker(db, "worker-1")
        async with sessions() as db:
            status = await inspect_worker_status(db)
            assert status["worker_available"] is True and status["active_workers"] == 1
        async with sessions.begin() as db:
            await db.execute(
                update(RunWorkerHeartbeat)
                .where(RunWorkerHeartbeat.id == "worker-1")
                .values(last_seen_at=shanghai_now(naive=True) - timedelta(seconds=30))
            )
        async with sessions() as db:
            assert (await inspect_worker_status(db))["worker_available"] is False
        async with sessions.begin() as db:
            await touch_worker(db, "worker-1")
        async with sessions() as db:
            assert (await inspect_worker_status(db))["worker_available"] is True
        async with sessions.begin() as db:
            await stop_worker(db, "worker-1")
        async with sessions() as db:
            assert (await inspect_worker_status(db))["worker_available"] is False
    finally:
        await engine.dispose()


async def exercise_busy_worker_heartbeat(database_path: Path, checkpoint_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证 worker 执行任务阻塞期间仍持续更新心跳。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def blocked_dispatch(*_args, **_kwargs):
        """模拟长时间运行中的调度任务。"""
        entered.set()
        await hold.wait()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
        monkeypatch.setattr(run_worker_module, "dispatch_next_run", blocked_dispatch)
        task = asyncio.create_task(
            run_worker_module.run_worker(
                checkpoint_path, idle_seconds=0.01, heartbeat_seconds=0.05, artifact_path=checkpoint_path.parent / "artifacts"
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            async with sessions() as db:
                started = await db.scalar(select(RunWorkerHeartbeat.last_seen_at))
            for _ in range(30):
                await asyncio.sleep(0.03)
                async with sessions() as db:
                    heartbeat = await db.scalar(select(RunWorkerHeartbeat.last_seen_at))
                if heartbeat > started:
                    break
            assert heartbeat > started
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        async with sessions() as db:
            assert (await db.scalar(select(RunWorkerHeartbeat.stopped_at))) is not None
            assert (await inspect_worker_status(db))["worker_available"] is False
    finally:
        await engine.dispose()


async def exercise_worker_health_api(database_path: Path) -> None:
    """验证公开健康探针和管理员详情接口的状态与权限。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as db:
        db.add_all(
            [
                Admin(id=1, username="worker-admin", password=hash_password("StrongPassword123"), role="admin", status=1, is_deleted=0),
                Admin(id=2, username="worker-user", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
            ]
        )
        await create_run(db, 2, input_text="waiting")

    async def test_session():
        """为健康接口测试提供独立数据库会话。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            unavailable = await client.get("/api/v1/health/worker")
            assert unavailable.status_code == 503 and unavailable.json()["data"] == {
                "worker_available": False,
            }
            assert (await client.get("/api/v1/runs/workers/status")).status_code == 401
            admin_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "worker-admin",
                    "password": "StrongPassword123",
                },
            )
            user_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "worker-user",
                    "password": "StrongPassword123",
                },
            )
            admin = {
                "Authorization": f"Bearer {admin_login.json()['data']['access_token']}",
            }
            user = {
                "Authorization": f"Bearer {user_login.json()['data']['access_token']}",
            }
            assert (await client.get("/api/v1/runs/workers/status", headers=user)).status_code == 403
            details = await client.get("/api/v1/runs/workers/status", headers=admin)
            assert details.status_code == 200 and details.json()["data"]["queued_runs"] == 1
            assert details.json()["data"]["active_workers"] == 0
            async with sessions.begin() as db:
                await register_worker(db, "worker-api")
            healthy = await client.get("/api/v1/health/worker")
            assert healthy.status_code == 200 and healthy.json()["data"] == {
                "worker_available": True,
            }
            assert (await client.get("/api/v1/runs/workers/status", headers=admin)).json()["data"]["active_workers"] == 1
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        await engine.dispose()


async def exercise_worker_restart_recovery(database_path: Path, checkpoint_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟执行器领取后失联，验证新 worker 启动时回收且不重放。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            run = await create_run(db, 1, input_text="可能已经调用过模型")
            old_token = (await claim_run(db, run.id)).lease_token
            await db.execute(
                update(AgentRun).where(AgentRun.id == run.id).values(lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1))
            )
        monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
        await run_worker_module.run_worker(checkpoint_path, once=True)
        async with sessions() as db:
            recovered = await db.get(AgentRun, run.id)
            assert recovered.state == "failed" and recovered.failure_code == "lease_expired"
            assert recovered.lease_token is None and recovered.lease_token != old_token
            events = await replay_events(db, run.id, 1)
            assert [event.state for event in events] == ["queued", "running", "failed"]
            assert events[-1].failure_code == "lease_expired"
        await run_worker_module.run_worker(checkpoint_path, once=True)
        async with sessions() as db:
            assert (await db.get(AgentRun, run.id)).event_seq == 3
    finally:
        await engine.dispose()


async def exercise_heartbeat_pruning(database_path: Path, checkpoint_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证旧心跳分批清理，并在 worker 启动时执行清理。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = shanghai_now(naive=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add_all(
                [
                    RunWorkerHeartbeat(
                        id="old-stopped",
                        started_at=now - timedelta(days=32),
                        last_seen_at=now - timedelta(days=31),
                        stopped_at=now - timedelta(days=31),
                    ),
                    RunWorkerHeartbeat(
                        id="old-crashed", started_at=now - timedelta(days=32), last_seen_at=now - timedelta(days=31), stopped_at=None
                    ),
                    RunWorkerHeartbeat(
                        id="recent-stopped",
                        started_at=now - timedelta(days=2),
                        last_seen_at=now - timedelta(days=1),
                        stopped_at=now - timedelta(days=1),
                    ),
                    RunWorkerHeartbeat(id="active", started_at=now, last_seen_at=now, stopped_at=None),
                ]
            )
        async with sessions.begin() as db:
            assert await prune_worker_heartbeats(db, limit=1, now=now) == 1
        async with sessions() as db:
            remaining = set((await db.scalars(select(RunWorkerHeartbeat.id))).all())
            assert len(remaining) == 3 and "active" in remaining and "recent-stopped" in remaining
        monkeypatch.setattr(run_worker_module, "AsyncSessionLocal", sessions)
        await run_worker_module.run_worker(checkpoint_path, once=True)
        async with sessions() as db:
            assert set((await db.scalars(select(RunWorkerHeartbeat.id))).all()) == {"active", "recent-stopped"}
            assert await prune_worker_heartbeats(db, now=now) == 0
            with pytest.raises(ValueError):
                await prune_worker_heartbeats(db, retention_days=0)
    finally:
        await engine.dispose()


async def exercise_worker_process_crash(database_path: Path, checkpoint_path: Path) -> None:
    """实际终止已领取任务的 worker，并验证重启后只回收租约。"""
    requested = threading.Event()
    release_response = threading.Event()
    requests_seen: list[str] = []

    class BlockingModelHandler(BaseHTTPRequestHandler):
        """在收到模型请求后阻塞响应，供测试终止独立 worker。"""

        def do_POST(self) -> None:
            """记录一次上游调用，并等待测试释放响应。"""
            requests_seen.append(self.path)
            requested.set()
            release_response.wait(timeout=15)
            try:
                self.send_response(503)
                self.end_headers()
            except BrokenPipeError:
                pass

        def log_message(self, format: str, *args: object) -> None:
            """避免测试模拟服务向控制台写入请求内容。"""

    server = ThreadingHTTPServer(("127.0.0.1", 0), BlockingModelHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    first_worker = None
    second_worker = None
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            db.add(Admin(id=1, username="crash-user", password="not-used", role="user", status=1, is_deleted=0))
            db.add(
                ProviderConnection(
                    id=1,
                    name="crash-provider",
                    name_key="crash-provider",
                    kind="ollama",
                    base_url=f"http://127.0.0.1:{server.server_port}/v1",
                    creator_id=1,
                    timeout_seconds=15,
                    status=1,
                    is_deleted=0,
                )
            )
            db.add(
                ModelCatalog(
                    id=1,
                    provider_id=1,
                    external_id="blocked-model",
                    display_name="Blocked Model",
                    creator_id=1,
                    capabilities=["text"],
                    verified_capabilities=[],
                    status=1,
                    is_deleted=0,
                )
            )
            run = await create_run(db, 1, model_id=1, input_text="阻塞模型调用")
        child_env = {
            **os.environ,
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database_path),
            "DEBUG": "false",
        }
        command = (sys.executable, "-m", "app.cli.run_worker", "--checkpoint-db", str(checkpoint_path), "--once")
        first_worker = await asyncio.create_subprocess_exec(
            *command, env=child_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        for _ in range(200):
            if requested.is_set() or first_worker.returncode is not None:
                break
            await asyncio.sleep(0.05)
        if not requested.is_set():
            error = (await first_worker.communicate())[1].decode() if first_worker.returncode is not None else "模型请求未开始"
            raise AssertionError(error)
        async with sessions() as db:
            active = await db.get(AgentRun, run.id)
            assert active.state == "running" and active.lease_token is not None
        first_worker.kill()
        await asyncio.wait_for(first_worker.wait(), timeout=5)
        async with sessions.begin() as db:
            active = await db.get(AgentRun, run.id)
            assert active.state == "running"
            await db.execute(
                update(AgentRun).where(AgentRun.id == run.id).values(lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1))
            )
        release_response.set()
        second_worker = await asyncio.create_subprocess_exec(
            *command, env=child_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, error_output = await asyncio.wait_for(second_worker.communicate(), timeout=15)
        assert second_worker.returncode == 0, error_output.decode()
        async with sessions() as db:
            recovered = await db.get(AgentRun, run.id)
            assert recovered.state == "failed" and recovered.failure_code == "lease_expired"
            assert recovered.event_seq == 3 and recovered.lease_token is None
            assert [event.state for event in await replay_events(db, run.id, 1)] == ["queued", "running", "failed"]
            assert [
                message.role for message in (await db.scalars(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id))).all()
            ] == ["user"]
        assert requests_seen == ["/v1/chat/completions"]
    finally:
        release_response.set()
        for worker in (first_worker, second_worker):
            if worker is not None and worker.returncode is None:
                worker.kill()
                await worker.wait()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        await engine.dispose()


def test_worker_status(tmp_path: Path) -> None:
    """运行执行器心跳和队列状态测试。"""
    asyncio.run(exercise_worker_status(tmp_path / "status.db"))


def test_busy_worker_heartbeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行繁忙执行器的后台心跳测试。"""
    asyncio.run(exercise_busy_worker_heartbeat(tmp_path / "busy.db", tmp_path / "checkpoints.db", monkeypatch))


def test_worker_health_api(tmp_path: Path) -> None:
    """运行执行器健康探针和管理员权限测试。"""
    asyncio.run(exercise_worker_health_api(tmp_path / "health.db"))


def test_worker_restart_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行执行器重启后过期租约回收测试。"""
    asyncio.run(exercise_worker_restart_recovery(tmp_path / "recovery.db", tmp_path / "checkpoints.db", monkeypatch))


def test_heartbeat_pruning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行旧执行器心跳分批和启动清理测试。"""
    asyncio.run(exercise_heartbeat_pruning(tmp_path / "prune.db", tmp_path / "checkpoints.db", monkeypatch))


def test_worker_process_crash(tmp_path: Path) -> None:
    """运行真实子进程终止与重启回收测试。"""
    asyncio.run(exercise_worker_process_crash(tmp_path / "crash.db", tmp_path / "checkpoints.db"))
