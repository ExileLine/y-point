# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:59:32
# @Author  : yangyuexiong
# @File    : test_run_manager.py

"""验证运行状态、租约和事件持久化的关键边界。"""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.run_approval import RunApproval
from app.models.base import Base, shanghai_now
from app.services import run_manager as run_manager_module
from app.services.run_manager import (
    RunConflictError,
    RunNotFoundError,
    acknowledge_cancel,
    append_text_delta,
    claim_run,
    complete_run,
    create_approval_request,
    create_run,
    decide_approval,
    get_owned_run,
    heartbeat_run,
    expire_approvals,
    recover_expired_runs,
    replay_events,
    request_cancel,
    resume_approved_run,
)


async def exercise_run_manager(database_path: Path) -> None:
    """在独立数据库中验证状态转换、隔离和事件补发。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            first = await create_run(db, 1, request_key="submit-1", model_id=7)
            duplicate = await create_run(db, 1, request_key="submit-1", model_id=7)
            assert duplicate.id == first.id and duplicate.model_id == 7
            with pytest.raises(RunConflictError):
                await create_run(db, 1, request_key="submit-1", model_id=99)
            with pytest.raises(RunConflictError):
                await create_run(db, 1, request_key="submit-1", model_id=7, model_timeout_seconds=60)
            with pytest.raises(RunConflictError):
                await create_run(db, 1, thread_id=first.thread_id, model_id=7)
            run_id = first.id
        async with sessions.begin() as db:
            with pytest.raises(RunNotFoundError):
                await get_owned_run(db, run_id, 2)
            with pytest.raises(RunNotFoundError):
                await replay_events(db, run_id, 2)
            claimed = await claim_run(db, run_id)
            token = claimed.lease_token
            assert token and claimed.state == "running"
            with pytest.raises(RunConflictError):
                await claim_run(db, run_id)
            with pytest.raises(RunConflictError):
                await complete_run(db, run_id, "wrong-token")
            with pytest.raises(RunConflictError):
                await append_text_delta(db, run_id, "wrong-token", "private")
            heartbeat = await heartbeat_run(db, run_id, token)
            assert heartbeat.version == 2
            await create_approval_request(
                db, run_id, token, "checkpoint-1", [{"name": "search", "arguments_hash": "a" * 64}],
            )
            paused = await get_owned_run(db, run_id, 1)
            assert paused.state == "waiting_for_approval" and paused.lease_token is None
            with pytest.raises(RunConflictError):
                await complete_run(db, run_id, token)
        async with sessions.begin() as db:
            with pytest.raises(RunConflictError):
                await resume_approved_run(db, run_id)
            assert (await decide_approval(db, run_id, 1, "approve")).state == "approved"
            resumed = await resume_approved_run(db, run_id)
            assert resumed.thread_id == first.thread_id and resumed.lease_token != token
            completed = await complete_run(db, run_id, resumed.lease_token, checkpoint_id="checkpoint-2")
            assert completed.state == "completed" and completed.checkpoint_id == "checkpoint-2"
        async with sessions.begin() as db:
            events = await replay_events(db, run_id, 1, after=2, limit=20)
            assert [event.state for event in events] == ["waiting_for_approval", "running", "completed"]
            assert [event.sequence for event in events] == [3, 4, 5]
            assert await replay_events(db, run_id, 1, after=5) == []
            assert (await request_cancel(db, run_id, 1)).state == "completed"
            assert len((await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id))).all()) == 5
            next_run = await create_run(db, 1, thread_id=first.thread_id, model_id=7)
            assert next_run.id != first.id and next_run.thread_id == first.thread_id
            with pytest.raises(RunConflictError):
                await create_run(db, 2, thread_id=first.thread_id)
    finally:
        await engine.dispose()


async def exercise_cancel_and_recovery(database_path: Path) -> None:
    """验证取消确认、租约过期回收和旧执行器失效。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            queued = await create_run(db, 1)
            active = await create_run(db, 1)
            lost = await create_run(db, 1)
            assert (await request_cancel(db, queued.id, 1)).state == "cancelled"
            active_token = (await claim_run(db, active.id)).lease_token
            lost_token = (await claim_run(db, lost.id)).lease_token
            assert active_token and lost_token
            assert (await request_cancel(db, active.id, 1)).state == "cancel_requested"
            assert (await acknowledge_cancel(db, active.id, active_token)).state == "cancelled"
        async with sessions.begin() as db:
            await db.execute(update(AgentRun).where(AgentRun.id == lost.id).values(
                lease_expires_at=shanghai_now(naive=True) - timedelta(seconds=1)
            ))
        async with sessions.begin() as db:
            assert await recover_expired_runs(db) == 1
            assert (await get_owned_run(db, lost.id, 1)).failure_code == "lease_expired"
            with pytest.raises(RunConflictError):
                await complete_run(db, lost.id, lost_token)
            events = await replay_events(db, lost.id, 1)
            assert [event.state for event in events] == ["queued", "running", "failed"]
            assert events[-1].failure_code == "lease_expired"
            assert await recover_expired_runs(db) == 0
    finally:
        await engine.dispose()


async def exercise_atomic_submit_and_claim(database_path: Path) -> None:
    """验证未提交的调度意图不会留下记录，竞争领取只成功一次。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", connect_args={"timeout": 10})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as db:
            rolled_back = await create_run(db, 1, request_key="rollback")
            await db.rollback()
        async with sessions.begin() as db:
            assert await db.get(AgentRun, rolled_back.id) is None
            assert (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == rolled_back.id))).all() == []
            run = await create_run(db, 1, request_key="claim-once")

        async def try_claim() -> str:
            """使用独立会话模拟一个执行器争抢任务。"""
            async with sessions.begin() as db:
                try:
                    await claim_run(db, run.id)
                    return "claimed"
                except RunConflictError:
                    return "conflict"

        outcomes = await asyncio.gather(try_claim(), try_claim())
        assert sorted(outcomes) == ["claimed", "conflict"]
        async with sessions.begin() as db:
            assert [event.state for event in await replay_events(db, run.id, 1)] == ["queued", "running"]
    finally:
        await engine.dispose()


def test_run_lifecycle_and_replay(tmp_path: Path) -> None:
    """验证运行生命周期与断线游标回放。"""
    asyncio.run(exercise_run_manager(tmp_path / "lifecycle.db"))


def test_run_cancel_and_expired_lease(tmp_path: Path) -> None:
    """验证取消流程和进程遗留租约回收。"""
    asyncio.run(exercise_cancel_and_recovery(tmp_path / "recovery.db"))


def test_cancel_retries_heartbeat_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证心跳先推进版本号时取消仍能形成单个状态事件。"""

    async def exercise() -> None:
        """在独立事务中模拟取消与租约心跳竞争。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-race.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        original_change_state = run_manager_module._change_state
        attempts = 0

        async def racing_change_state(db, run, state, **kwargs):
            """第一次取消写入前模拟已有心跳推进版本号。"""
            nonlocal attempts
            if state == "cancel_requested" and attempts == 0:
                attempts += 1
                await db.execute(update(AgentRun).where(AgentRun.id == run.id).values(
                    version=AgentRun.version + 1,
                ).execution_options(synchronize_session=False))
            return await original_change_state(db, run, state, **kwargs)

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1)
                await claim_run(db, run.id)
            monkeypatch.setattr(run_manager_module, "_change_state", racing_change_state)
            async with sessions.begin() as db:
                cancelled = await request_cancel(db, run.id, 1)
                assert cancelled.state == "cancel_requested" and attempts == 1
            async with sessions() as db:
                events = await replay_events(db, run.id, 1)
                assert [event.state for event in events] == ["queued", "running", "cancel_requested"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_run_atomic_submit_and_claim(tmp_path: Path) -> None:
    """验证提交回滚和并发领取不会产生重复执行意图。"""
    asyncio.run(exercise_atomic_submit_and_claim(tmp_path / "atomic.db"))


async def exercise_approval_expiry(database_path: Path) -> None:
    """验证待处理审批到期与运行终态在同一事务中更新。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            run = await create_run(db, 1)
            token = (await claim_run(db, run.id)).lease_token
            approval = await create_approval_request(
                db, run.id, token, "checkpoint-1", [{"name": "search", "arguments_hash": "a" * 64}],
            )
            approval_id = approval.id
            with pytest.raises(RunConflictError):
                await resume_approved_run(db, run.id)
        async with sessions.begin() as db:
            await db.execute(update(RunApproval).where(RunApproval.id == approval_id).values(
                expires_at=shanghai_now(naive=True) - timedelta(seconds=1),
            ))
            assert await expire_approvals(db) == 1
        async with sessions.begin() as db:
            assert (await db.get(RunApproval, approval_id)).state == "expired"
            timed_out = await get_owned_run(db, run.id, 1)
            assert timed_out.state == "timed_out" and timed_out.failure_code == "approval_expired"
            assert await expire_approvals(db) == 0
    finally:
        await engine.dispose()


def test_approval_expiry(tmp_path: Path) -> None:
    """运行审批到期与运行终态一致性测试。"""
    asyncio.run(exercise_approval_expiry(tmp_path / "approval-expiry.db"))
