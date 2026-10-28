# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:18:34
# @Author  : yangyuexiong
# @File    : run_worker.py

"""运行独立的 M3 持久化文本任务执行器。"""

import argparse
import asyncio
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from loguru import logger

from app.db.session import AsyncSessionLocal, close_db
from app.services.artifact_store import artifact_root, prune_artifacts, publish_pending_answers
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_manager import expire_approvals, expire_queued_runs, recover_expired_runs
from app.services.run_reconciler import reconcile_expired_runs
from app.services.runtime_checkpoint import open_async_checkpointer
from app.services.worker_monitor import WORKER_STALE_SECONDS, prune_worker_heartbeats, register_worker, stop_worker, touch_worker
from app.services.worker_tools import make_worker_tool_resolver

DEFAULT_CHECKPOINT_PATH = Path(__file__).resolve().parents[2] / ".run-private" / "checkpoints.sqlite"
HEARTBEAT_PRUNE_INTERVAL_SECONDS = 3600


async def _heartbeat_loop(worker_id: str, heartbeat_seconds: float) -> None:
    """在空闲和执行模型期间持续更新独立进程心跳。"""
    while True:
        await asyncio.sleep(heartbeat_seconds)
        try:
            async with AsyncSessionLocal.begin() as db:
                await touch_worker(db, worker_id)
        except Exception as exc:
            logger.error("执行器心跳写入失败：{}", type(exc).__name__)


async def run_worker(
    checkpoint_path: Path, *, once: bool = False, idle_seconds: float = 2, heartbeat_seconds: float = 5, artifact_path: Path | None = None
) -> None:
    """循环回收遗留租约并执行最早的排队任务。"""
    if idle_seconds <= 0 or not 0 < heartbeat_seconds < WORKER_STALE_SECONDS / 2:
        raise ValueError("空闲轮询间隔或执行器心跳间隔无效")
    async with open_async_checkpointer(checkpoint_path) as checkpointer:
        await checkpointer.setup()
    worker_id = str(uuid4()) if not once else None
    heartbeat_task = None
    if worker_id is not None:
        async with AsyncSessionLocal.begin() as db:
            await register_worker(db, worker_id)
        heartbeat_task = asyncio.create_task(_heartbeat_loop(worker_id, heartbeat_seconds))
    try:
        last_prune_at = None
        while True:
            try:
                reconciled = await reconcile_expired_runs(AsyncSessionLocal, checkpoint_path)
                prune_due = last_prune_at is None or time.monotonic() - last_prune_at >= HEARTBEAT_PRUNE_INTERVAL_SECONDS
                async with AsyncSessionLocal.begin() as db:
                    recovered = await recover_expired_runs(db)
                    expired = await expire_approvals(db)
                    queue_expired = await expire_queued_runs(db)
                    pruned = await prune_worker_heartbeats(db) if prune_due else 0
                if prune_due:
                    last_prune_at = time.monotonic()
                if recovered:
                    logger.warning("已回收 {} 个过期运行", recovered)
                if reconciled:
                    logger.info("已从检查点补偿 {} 个已完成运行", reconciled)
                if expired:
                    logger.info("已关闭 {} 个到期审批", expired)
                if queue_expired:
                    logger.info("已关闭 {} 个排队超时运行", queue_expired)
                if pruned:
                    logger.info("已清理 {} 条过期执行器心跳", pruned)
                if prune_due and not once:
                    try:
                        root = artifact_path or artifact_root()
                        root.mkdir(mode=0o700, parents=True, exist_ok=True)
                        expired_files, orphan_files = await prune_artifacts(AsyncSessionLocal, root)
                        if expired_files or orphan_files:
                            logger.info("已清理 {} 个到期文件及 {} 个孤儿文件", expired_files, orphan_files)
                    except Exception as exc:
                        logger.error("文件清理失败：{}", type(exc).__name__)
                run = await dispatch_next_run(
                    AsyncSessionLocal, checkpoint_path, resolve_tools=make_worker_tool_resolver(AsyncSessionLocal)
                )
                if run is not None:
                    logger.info("运行 {} 结束，状态 {}", run.id, run.state)
                root = artifact_path or artifact_root()
                published = await publish_pending_answers(AsyncSessionLocal, root)
                if published:
                    logger.info("已发布 {} 个最终回答产物", published)
            except Exception as exc:
                # 独立执行器只记录异常类别，避免将凭据或消息正文写入日志。
                logger.error("运行调度失败：{}", type(exc).__name__)
                if once:
                    raise
                await asyncio.sleep(idle_seconds)
                continue
            if once:
                return
            if run is None:
                await asyncio.sleep(idle_seconds)
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        if worker_id is not None:
            try:
                async with AsyncSessionLocal.begin() as db:
                    await stop_worker(db, worker_id)
            except Exception as exc:
                logger.error("执行器退出状态写入失败：{}", type(exc).__name__)


async def _serve(checkpoint_path: Path, *, once: bool, idle_seconds: float, heartbeat_seconds: float) -> None:
    """在执行器退出时于同一事件循环关闭数据库连接池。"""
    try:
        await run_worker(checkpoint_path, once=once, idle_seconds=idle_seconds, heartbeat_seconds=heartbeat_seconds)
    finally:
        await close_db()


def main() -> None:
    """解析检查点文件位置并启动独立执行进程。"""
    parser = argparse.ArgumentParser(description="运行 M3 持久化文本任务执行器")
    parser.add_argument(
        "--checkpoint-db",
        type=Path,
        default=DEFAULT_CHECKPOINT_PATH,
        help=f"SQLite 后端使用的私有检查点文件（默认：{DEFAULT_CHECKPOINT_PATH}）",
    )
    parser.add_argument("--once", action="store_true", help="只扫描并处理一轮")
    parser.add_argument("--idle-seconds", type=float, default=2, help="无任务时的轮询间隔秒数")
    parser.add_argument("--heartbeat-seconds", type=float, default=5, help="执行器写入存活心跳的间隔秒数")
    args = parser.parse_args()
    asyncio.run(_serve(args.checkpoint_db, once=args.once, idle_seconds=args.idle_seconds, heartbeat_seconds=args.heartbeat_seconds))


if __name__ == "__main__":
    main()
