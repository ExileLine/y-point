# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:17:15
# @Author  : yangyuexiong
# @File    : tool_resources.py

"""核对工具临时资源、应用容量预留及历史环境引用，避免强杀后残留。"""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.core.config import BASE_DIR, get_config
from app.core.enums import RunState, ToolInvocationState
from app.models.agent_run import AgentRun
from app.models.base import shanghai_now
from app.models.tool import ToolInvocation, UserToolVersion
from app.models.tool_execution import ToolExecutionLease

BUILD_RESERVATION_BYTES = 512 * 1024 * 1024
RESOURCE_MARKER = ".yp-resource.json"


class ToolStorageCapacityError(ValueError):
    """存储预算或剩余磁盘不满足准备条件。"""


def dependency_root() -> Path:
    """返回所有 API、worker 必须共享的依赖缓存目录。"""
    path = Path(get_config().SCRIPT_DEPENDENCY_STORAGE_DIR).expanduser()
    return (path if path.is_absolute() else BASE_DIR / path).resolve()


def resource_scope() -> str:
    """同一共享目录使用相同资源标签，隔离测试与其他部署。"""
    return hashlib.sha256(str(dependency_root()).encode()).hexdigest()[:24]


def resource_labels(timeout_seconds: int) -> list[str]:
    """容器和网络保存可核对的作用域及最晚清理时间。"""
    return [
        "--label",
        "y-point.managed=tool",
        "--label",
        f"y-point.scope={resource_scope()}",
        "--label",
        f"y-point.deadline={int(time.time()) + timeout_seconds + 30}",
    ]


def _directory_bytes(path: Path) -> int:
    """计算实际文件字节，不追随链接进入共享目录外部。"""
    total = 0
    for directory, _, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except FileNotFoundError:
                pass
    return total


def storage_status() -> dict:
    """返回应用预算、预留和文件系统余量，避免误称为操作系统硬配额。"""
    root = dependency_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    occupied = 0
    for child in root.iterdir():
        size = _directory_bytes(child) if child.is_dir() and not child.is_symlink() else child.lstat().st_size
        reserved = 0
        marker = child / RESOURCE_MARKER
        if child.is_dir() and not child.is_symlink() and marker.is_file():
            try:
                reserved = int(json.loads(marker.read_text())["reserved_bytes"])
            except (OSError, ValueError, KeyError, TypeError):
                reserved = BUILD_RESERVATION_BYTES
        occupied += max(size, reserved)
    config = get_config()
    free = shutil.disk_usage(root).free
    return {
        "accounted_bytes": occupied,
        "budget_bytes": config.TOOL_STORAGE_MAX_BYTES,
        "free_bytes": free,
        "min_free_bytes": config.TOOL_STORAGE_MIN_FREE_BYTES,
        "warning": occupied >= config.TOOL_STORAGE_MAX_BYTES * 0.8 or free < config.TOOL_STORAGE_MIN_FREE_BYTES,
    }


def mark_resource_directory(directory: Path, timeout_seconds: int, *, reserve_bytes: int = 0) -> None:
    """为临时目录和待发布缓存记录清理边界，覆盖重命名后尚未提交数据库时的强杀窗口。"""
    (directory / RESOURCE_MARKER).write_text(
        json.dumps(
            {
                "deadline": int(time.time()) + timeout_seconds + 30,
                "reserved_bytes": reserve_bytes,
                "scope": resource_scope(),
            },
        ),
    )


@contextmanager
def managed_workspace(root: Path, prefix: str, timeout_seconds: int, *, reserve_bytes: int = 0):
    """在共享文件锁下预留最坏构建空间，所有退出路径清除本轮目录。"""
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = dependency_root() / ".capacity.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = None
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if reserve_bytes:
            status = storage_status()
            if status["accounted_bytes"] + reserve_bytes > status["budget_bytes"] or (status["free_bytes"] - reserve_bytes < status["min_free_bytes"]):
                logger.warning("工具依赖存储预算或剩余容量不足，拒绝新的准备任务")
                raise ToolStorageCapacityError("依赖存储容量不足，请联系维护者清理未引用资源或扩容")
        directory = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
        mark_resource_directory(directory, timeout_seconds, reserve_bytes=reserve_bytes)
    try:
        yield str(directory)
    finally:
        shutil.rmtree(directory)


async def _docker_metadata(*arguments: str, missing_ok: bool = False) -> bytes:
    """读取有界 Docker 元数据，故障向调用者报告并留待下轮重试。"""
    from app.services.script_sandbox import _read_bounded

    process = await asyncio.create_subprocess_exec("docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, errors, _ = await asyncio.wait_for(asyncio.gather(_read_bounded(process.stdout), _read_bounded(process.stderr), process.wait()), timeout=10)
        if process.returncode:
            if missing_ok and any(marker in errors.lower() for marker in (b"no such", b"not found")):
                return b""
            raise OSError("工具 Docker 资源核对失败")
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _prune_directories(referenced: set[str], active_lease: bool, scope: str, cutoff: float) -> int:
    """在线程中持共享文件锁删除过期目录，避免磁盘扫描和锁等待阻塞 worker 心跳。"""
    removed = 0
    root = dependency_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    sandbox_root = Path(get_config().SCRIPT_SANDBOX_STORAGE_DIR).expanduser()
    if not sandbox_root.is_absolute():
        sandbox_root = BASE_DIR / sandbox_root
    with (root / ".capacity.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for parent in (root, sandbox_root):
            if not parent.is_dir():
                continue
            for path in parent.iterdir():
                if not path.is_dir() or path.is_symlink() or path.name in referenced:
                    continue
                marker = path / RESOURCE_MARKER
                if not marker.is_file():
                    continue
                try:
                    metadata = json.loads(marker.read_text())
                    expired = metadata["scope"] == scope and metadata["deadline"] < cutoff
                except (ValueError, KeyError, OSError, TypeError):
                    continue
                if expired and not active_lease:
                    shutil.rmtree(path)
                    removed += 1
    return removed


async def prune_tool_resources(sessions: async_sessionmaker[AsyncSession]) -> dict:
    """仅回收本部署已过期资源，保留所有历史版本引用的环境。"""
    config = get_config()
    cutoff = time.time() - config.TOOL_RESOURCE_GRACE_SECONDS
    scope = resource_scope()
    removed = {
        "containers": 0,
        "networks": 0,
        "directories": 0,
        "unknown_invocations": 0,
    }
    async with sessions.begin() as db:
        referenced = set((await db.scalars(select(UserToolVersion.environment_key).where(UserToolVersion.environment_key.is_not(None)))).all())
        ended_runs = select(AgentRun.id).where(AgentRun.state.in_([RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.TIMED_OUT]))
        result = await db.execute(
            update(ToolInvocation)
            .where(ToolInvocation.state == ToolInvocationState.RUNNING, ToolInvocation.run_id.in_(ended_runs))
            .values(state=ToolInvocationState.UNKNOWN, failure_code="worker_interrupted"),
        )
        removed["unknown_invocations"] = result.rowcount
        building = await db.scalar(select(ToolExecutionLease.id).where(ToolExecutionLease.expires_at > shanghai_now(naive=True)).limit(1))
    filters = ["--filter", "label=y-point.managed=tool", "--filter", f"label=y-point.scope={scope}"]
    for category, listing in (("containers", ("ps", "-aq")), ("networks", ("network", "ls", "-q"))):
        identifiers = (await _docker_metadata(*listing, *filters)).decode().split()
        for identifier in identifiers:
            if re.fullmatch(r"[a-f0-9]{12,64}", identifier) is None:
                continue
            inspect_args = ("inspect", identifier) if category == "containers" else ("network", "inspect", identifier)
            metadata = await _docker_metadata(*inspect_args, missing_ok=True)
            if not metadata:
                continue
            item = json.loads(metadata)[0]
            labels = item.get("Config", {}).get("Labels", {}) if category == "containers" else item.get("Labels", {})
            if labels.get("y-point.scope") != scope or labels.get("y-point.managed") != "tool":
                continue
            try:
                deadline = int(labels["y-point.deadline"])
            except (KeyError, ValueError):
                continue
            if deadline >= cutoff:
                continue
            command = ("rm", "-f", identifier) if category == "containers" else ("network", "rm", identifier)
            if await _docker_metadata(*command, missing_ok=True):
                removed[category] += 1
    removed["directories"] = await asyncio.to_thread(_prune_directories, referenced, building is not None, scope, cutoff)
    status = await asyncio.to_thread(storage_status)
    if status["warning"]:
        logger.warning(
            "工具依赖存储告警：预留及占用 {} 字节，预算 {} 字节，可用 {} 字节",
            status["accounted_bytes"],
            status["budget_bytes"],
            status["free_bytes"],
        )
    return {
        **removed,
        "storage": status,
    }
