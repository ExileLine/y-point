# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 10:02:24
# @Author  : yangyuexiong
# @File    : worker_monitor.py

"""记录 worker 心跳并查询执行器与队列状态。"""

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.base import shanghai_datetime, shanghai_now
from app.models.run_worker_heartbeat import RunWorkerHeartbeat

WORKER_STALE_SECONDS = 20
WORKER_HEARTBEAT_RETENTION_DAYS = 30


async def register_worker(db: AsyncSession, worker_id: str) -> None:
    """为一次执行器启动创建独立心跳记录。"""
    now = shanghai_now(naive=True)
    db.add(RunWorkerHeartbeat(id=worker_id, started_at=now, last_seen_at=now))
    await db.flush()


async def touch_worker(db: AsyncSession, worker_id: str) -> None:
    """更新仍在运行的执行器心跳。"""
    result = await db.execute(
        update(RunWorkerHeartbeat)
        .where(RunWorkerHeartbeat.id == worker_id, RunWorkerHeartbeat.stopped_at.is_(None))
        .values(last_seen_at=shanghai_now(naive=True))
    )
    if result.rowcount != 1:
        raise RuntimeError("执行器心跳记录不存在或已停止")


async def stop_worker(db: AsyncSession, worker_id: str) -> None:
    """将正常退出的执行器从存活列表移除。"""
    now = shanghai_now(naive=True)
    await db.execute(
        update(RunWorkerHeartbeat)
        .where(RunWorkerHeartbeat.id == worker_id, RunWorkerHeartbeat.stopped_at.is_(None))
        .values(last_seen_at=now, stopped_at=now)
    )


async def prune_worker_heartbeats(
    db: AsyncSession, *, retention_days: int = WORKER_HEARTBEAT_RETENTION_DAYS, limit: int = 1000, now: datetime | None = None
) -> int:
    """分批删除保留期外的已停止或失联执行器心跳。"""
    if retention_days <= 0 or not 1 <= limit <= 1000:
        raise ValueError("无效的心跳保留时长或清理数量")
    cutoff = (now or shanghai_now(naive=True)) - timedelta(days=retention_days)
    expired_ids = (
        select(RunWorkerHeartbeat.id)
        .where(RunWorkerHeartbeat.last_seen_at < cutoff)
        .order_by(RunWorkerHeartbeat.last_seen_at, RunWorkerHeartbeat.id)
        .limit(limit)
    )
    result = await db.execute(delete(RunWorkerHeartbeat).where(RunWorkerHeartbeat.id.in_(expired_ids)))
    return result.rowcount


async def worker_available(db: AsyncSession, *, stale_after_seconds: int = WORKER_STALE_SECONDS) -> bool:
    """只查询有效心跳，供公开健康探针使用。"""
    if stale_after_seconds <= 0:
        raise ValueError("心跳过期时长必须大于零")
    cutoff = shanghai_now(naive=True) - timedelta(seconds=stale_after_seconds)
    worker_id = await db.scalar(
        select(RunWorkerHeartbeat.id).where(RunWorkerHeartbeat.stopped_at.is_(None), RunWorkerHeartbeat.last_seen_at >= cutoff).limit(1)
    )
    return worker_id is not None


async def inspect_worker_status(db: AsyncSession, *, stale_after_seconds: int = WORKER_STALE_SECONDS, now: datetime | None = None) -> dict:
    """统计有效心跳、排队等待和租约已过期的运行。"""
    if stale_after_seconds <= 0:
        raise ValueError("心跳过期时长必须大于零")
    now = now or shanghai_now(naive=True)
    cutoff = now - timedelta(seconds=stale_after_seconds)
    active_workers = await db.scalar(
        select(func.count())
        .select_from(RunWorkerHeartbeat)
        .where(RunWorkerHeartbeat.stopped_at.is_(None), RunWorkerHeartbeat.last_seen_at >= cutoff)
    )
    latest_heartbeat_at = await db.scalar(select(func.max(RunWorkerHeartbeat.last_seen_at)))
    queued_runs = await db.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.state == "queued"))
    oldest_queued_at = await db.scalar(select(func.min(AgentRun.create_time)).where(AgentRun.state == "queued"))
    expired_running_runs = await db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.state.in_(("running", "cancel_requested")), AgentRun.lease_expires_at <= now)
    )
    oldest_queued_seconds = None
    if oldest_queued_at is not None:
        oldest_queued_seconds = max(0, int((now - shanghai_datetime(oldest_queued_at, naive=True)).total_seconds()))
    return {
        "worker_available": bool(active_workers),
        "active_workers": active_workers,
        "latest_heartbeat_at": latest_heartbeat_at,
        "queued_runs": queued_runs,
        "oldest_queued_seconds": oldest_queued_seconds,
        "expired_running_runs": expired_running_runs,
    }
