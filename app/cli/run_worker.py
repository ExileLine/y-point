# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:18:34
# @Author  : yangyuexiong
# @File    : run_worker.py

"""运行独立的 M3 持久化文本任务执行器。"""

# ruff: noqa: E402

import argparse
import asyncio
import os
import signal
import sys
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from loguru import logger

from app.core.config import BASE_DIR, BaseConfig, get_config, normalize_env, resolve_env_files
from app.core.worker_logging import WorkerLogCapture


def _exit_on_termination(signum: int, _frame: object) -> None:
    """将启动器的终止信号转为正常异常展开，执行退出时的日志排空。"""
    raise SystemExit(128 + signum)


def _redirect_output_to_log() -> None:
    """在加载业务模块前收集执行器输出，按容量循环保留最新日志。"""
    env_name = normalize_env(os.getenv("FAST_API_ENV"))
    config = BaseConfig(_env_file=[str(path) for path in resolve_env_files(env_name)])
    log_path = Path(config.WORKER_LOG_FILE).expanduser()
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    WorkerLogCapture(log_path, config.WORKER_LOG_MAX_BYTES)
    signal.signal(signal.SIGTERM, _exit_on_termination)


# 业务模块导入时会初始化数据库日志，因此先重定向输出。
if __name__ == "__main__" and not {"-h", "--help"}.intersection(sys.argv[1:]):
    _redirect_output_to_log()

from app.db.session import AsyncSessionLocal, close_db
from app.services.artifact_store import artifact_root, prune_artifacts, publish_pending_answers
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_manager import expire_approvals, expire_queued_runs, recover_expired_runs
from app.services.run_reconciler import reconcile_expired_runs
from app.services.runtime_checkpoint import open_async_checkpointer
from app.services.worker_monitor import WORKER_STALE_SECONDS, prune_worker_heartbeats, register_worker, stop_worker, touch_worker
from app.services.worker_tools import make_worker_tool_resolver
from app.services.tool_environments import prepare_next_tool_environment
from app.services.tool_resources import prune_tool_resources
from app.services.knowledge_jobs import process_next_knowledge_job, prune_deleted_knowledge_blobs

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


async def _tool_environment_loop(idle_seconds: float) -> None:
    """独立准备依赖，避免下载和安装阻塞对话调度。"""
    while True:
        try:
            prepared = await prepare_next_tool_environment(AsyncSessionLocal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("依赖环境调度失败：{}", type(exc).__name__)
            prepared = False
        if not prepared:
            await asyncio.sleep(idle_seconds)


async def _tool_resource_loop() -> None:
    """定期核对强杀残留与容量，不依赖对话队列是否空闲。"""
    while True:
        try:
            resources = await prune_tool_resources(AsyncSessionLocal)
            if any(resources[key] for key in ("containers", "networks", "directories", "unknown_invocations")):
                logger.info("工具资源核对完成：{}", resources)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("工具资源核对失败：{}", type(exc).__name__)
        await asyncio.sleep(get_config().TOOL_RESOURCE_RECONCILE_SECONDS)


async def _knowledge_loop(idle_seconds: float) -> None:
    """独立处理知识索引与已删除原文，不阻塞对话调度和进程心跳。"""
    while True:
        try:
            await prune_deleted_knowledge_blobs(AsyncSessionLocal)
            processed = await process_next_knowledge_job(AsyncSessionLocal)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("知识索引调度失败：{}", type(exc).__name__)
            processed = False
        if not processed:
            await asyncio.sleep(idle_seconds)


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
    environment_task = None
    resource_task = None
    knowledge_task = None
    if worker_id is not None:
        async with AsyncSessionLocal.begin() as db:
            await register_worker(db, worker_id)
        heartbeat_task = asyncio.create_task(_heartbeat_loop(worker_id, heartbeat_seconds))
        resource_task = asyncio.create_task(_tool_resource_loop())
        knowledge_task = asyncio.create_task(_knowledge_loop(idle_seconds))
    try:
        last_prune_at = None
        while True:
            try:
                reconciled = await reconcile_expired_runs(AsyncSessionLocal, checkpoint_path)
                if once:
                    await prepare_next_tool_environment(AsyncSessionLocal)
                    await prune_deleted_knowledge_blobs(AsyncSessionLocal)
                    await process_next_knowledge_job(AsyncSessionLocal)
                elif environment_task is None:
                    environment_task = asyncio.create_task(_tool_environment_loop(idle_seconds))
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
        if knowledge_task is not None:
            knowledge_task.cancel()
            with suppress(asyncio.CancelledError):
                await knowledge_task
        if resource_task is not None:
            resource_task.cancel()
            with suppress(asyncio.CancelledError):
                await resource_task
        if environment_task is not None:
            environment_task.cancel()
            with suppress(asyncio.CancelledError):
                await environment_task
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
