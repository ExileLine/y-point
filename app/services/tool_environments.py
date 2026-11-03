# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 22:33:20
# @Author  : yangyuexiong
# @File    : tool_environments.py

"""为用户声明的 Python 依赖准备可复用、只读且固定版本的运行环境。"""

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import BASE_DIR, get_config
from app.core.enums import ToolEnvironmentFailure, ToolEnvironmentState, ToolLeaseKind
from app.models.base import shanghai_now
from app.models.tool import UserTool, UserToolVersion
from app.services.tool_resources import (
    BUILD_RESERVATION_BYTES,
    ToolStorageCapacityError,
    managed_workspace,
    mark_resource_directory,
    resource_labels,
)
from app.services.tool_execution import acquire_tool_slot, release_tool_slot, ToolCapacityError
from app.services.script_sandbox import ScriptEnvironment, SandboxConfigurationError, _read_bounded, _remove_container

MAX_ENVIRONMENT_BYTES = 512 * 1024 * 1024
MAX_ENVIRONMENT_FILES = 20000
ENVIRONMENT_FIELDS = (
    "environment_state",
    "environment_spec_hash",
    "environment_key",
    "environment_image",
    "environment_lock",
    "environment_manifest",
    "environment_error_code",
    "environment_error_detail",
)
FAILURE_MESSAGES = {
    ToolEnvironmentFailure.IMAGE_UNAVAILABLE: "基础镜像不可用，请联系维护者准备沙箱镜像后重试",
    ToolEnvironmentFailure.DEPENDENCIES_UNAVAILABLE: "依赖获取失败：请检查包名、版本约束、包源及兼容 wheel；离线部署请补充安装包后重试",
    ToolEnvironmentFailure.INSTALL_FAILED: "依赖安装失败，请检查依赖版本兼容性及镜像中的系统库",
    ToolEnvironmentFailure.BUILD_TIMEOUT: "依赖环境准备超时，可稍后重试或缩小依赖范围",
    ToolEnvironmentFailure.SIZE_LIMIT: "依赖环境超过文件数量或容量限制",
    ToolEnvironmentFailure.INVALID_ARTIFACT: "依赖环境文件缺失或校验失败，请重新准备环境",
    ToolEnvironmentFailure.WORKER_INTERRUPTED: "环境准备任务被中断，请重试",
    ToolEnvironmentFailure.CONFIGURATION_ERROR: "依赖包源或离线安装目录配置不可用，请联系维护者",
}


class EnvironmentBuildError(ValueError):
    """携带可持久化的脱敏失败类别，不公开 pip 输出和仓库凭据。"""

    def __init__(self, code: ToolEnvironmentFailure, detail: str | None = None) -> None:
        """保存受控错误类别及经过校验的依赖名称。"""
        super().__init__(FAILURE_MESSAGES[code])
        self.code = code
        self.detail = detail


def normalize_requirements(items: list[str] | None) -> list[str]:
    """规范化标准包声明，拒绝 URL、文件路径、pip 参数和过大的输入。"""
    if items is None:
        return []
    if not isinstance(items, list) or len(items) > 32:
        raise ValueError("Python 依赖最多 32 项")
    result = set()
    for item in items:
        if not isinstance(item, str) or not 1 <= len(item.encode()) <= 256 or "\n" in item or "\r" in item:
            raise ValueError("每项依赖必须是 256 字节内的单行包声明")
        try:
            requirement = Requirement(item.strip())
        except InvalidRequirement as exc:
            raise ValueError("依赖需使用标准包名和版本约束，不接受文件路径或 pip 选项") from exc
        if requirement.url is not None:
            raise ValueError("依赖不接受 URL、Git 或本地文件引用")
        requirement.name = canonicalize_name(requirement.name)
        result.add(str(requirement))
    return sorted(result)


def initial_environment_fields(requirements: list[str], previous: UserToolVersion | None = None) -> dict:
    """依赖不变时复用已完成的环境，其余版本进入持久化准备队列。"""
    fields = {key: None for key in ENVIRONMENT_FIELDS}
    fields["environment_state"] = ToolEnvironmentState.PENDING if requirements else ToolEnvironmentState.NOT_REQUIRED
    if (
        requirements
        and previous is not None
        and previous.requirements == requirements
        and previous.environment_state == ToolEnvironmentState.READY
    ):
        fields.update({key: getattr(previous, key) for key in ENVIRONMENT_FIELDS})
    return fields


def environment_output(version: UserToolVersion) -> dict:
    """只向 Tool 所有者返回准备状态、依赖锁及脱敏失败说明。"""
    message = FAILURE_MESSAGES.get(version.environment_error_code)
    if message and version.environment_error_detail:
        message += f"（{version.environment_error_detail}）"
    return {
        "state": version.environment_state,
        "failure_code": version.environment_error_code,
        "message": message,
        "locked_requirements": version.environment_lock or [],
    }


def environment_root() -> Path:
    """解析仅由服务端配置的环境存储目录。"""
    root = Path(get_config().SCRIPT_DEPENDENCY_STORAGE_DIR).expanduser()
    root = root if root.is_absolute() else BASE_DIR / root
    if root.is_symlink():
        raise EnvironmentBuildError(ToolEnvironmentFailure.CONFIGURATION_ERROR)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root.resolve()


def _file_manifest(root: Path) -> list[dict]:
    """计算安装文件摘要，同时拒绝软链接及超限环境。"""
    manifest = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise EnvironmentBuildError(ToolEnvironmentFailure.INVALID_ARTIFACT)
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        if total > MAX_ENVIRONMENT_BYTES or len(manifest) >= MAX_ENVIRONMENT_FILES:
            raise EnvironmentBuildError(ToolEnvironmentFailure.SIZE_LIMIT)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest.append(
            {
                "path": path.relative_to(root).as_posix(),
                "bytes": size,
                "sha256": digest,
            }
        )
    return manifest


def _verify_environment(key: str, manifest: list[dict]) -> Path:
    """确认缓存目录完整且未被替换后返回可只读挂载的目录。"""
    if re.fullmatch(r"[a-f0-9]{64}", key or "") is None or not isinstance(manifest, list):
        raise EnvironmentBuildError(ToolEnvironmentFailure.INVALID_ARTIFACT)
    path = environment_root() / key
    if path.is_symlink() or not path.is_dir() or _file_manifest(path) != manifest:
        raise EnvironmentBuildError(ToolEnvironmentFailure.INVALID_ARTIFACT)
    return path


async def require_script_environment(version: UserToolVersion) -> ScriptEnvironment | None:
    """执行前验证该 Tool 版本的依赖状态、固定镜像及文件摘要。"""
    if not version.requirements:
        return None
    if version.environment_state != ToolEnvironmentState.READY:
        raise SandboxConfigurationError("依赖环境尚未准备成功，请在 Tool 页面查看进度或重试")
    try:
        if re.fullmatch(r"sha256:[a-f0-9]{64}", version.environment_image or "") is None:
            raise EnvironmentBuildError(ToolEnvironmentFailure.INVALID_ARTIFACT)
        site = await asyncio.to_thread(_verify_environment, version.environment_key, version.environment_manifest)
    except (EnvironmentBuildError, OSError) as exc:
        raise SandboxConfigurationError("依赖环境缺失或校验失败，请重新准备环境") from exc
    return ScriptEnvironment(version.environment_image, site)


async def _docker_output(*arguments: str) -> bytes:
    """读取 Docker 元数据，限制输出和执行时限。"""
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError as exc:
        raise EnvironmentBuildError(ToolEnvironmentFailure.IMAGE_UNAVAILABLE) from exc
    try:
        output, _ = await asyncio.wait_for(asyncio.gather(_read_bounded(process.stdout), process.wait()), timeout=10)
        if process.returncode:
            raise EnvironmentBuildError(ToolEnvironmentFailure.IMAGE_UNAVAILABLE)
        return output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _check_build_size(root: Path) -> None:
    """准备期间限制挂载目录容量，避免下载填满宿主存储。"""
    total = 0
    count = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise EnvironmentBuildError(ToolEnvironmentFailure.INVALID_ARTIFACT)
        if path.is_file():
            count += 1
            total += path.stat().st_size
        if total > MAX_ENVIRONMENT_BYTES or count > MAX_ENVIRONMENT_FILES:
            raise EnvironmentBuildError(ToolEnvironmentFailure.SIZE_LIMIT)


def _missing_dependency(errors: bytes) -> str | None:
    """仅从 pip 输出提取标准依赖声明，避免泄漏仓库地址和凭据。"""
    match = re.search(rb"No matching distribution found for ([^\r\n]+)", errors)
    if match:
        try:
            values = normalize_requirements([match.group(1).decode("utf-8")])
            return values[0]
        except (ValueError, UnicodeDecodeError):
            pass
    return None


@asynccontextmanager
async def _package_network(image: str):
    """通过隔离网络和域名白名单代理获取 wheel，退出时回收临时网络。"""
    config = get_config()
    host = urlsplit(config.SCRIPT_DEPENDENCY_INDEX_URL).hostname
    allowed = sorted({item.lower().rstrip(".") for item in [*config.SCRIPT_DEPENDENCY_ALLOWED_HOSTS, host]})
    if any(not isinstance(item, str) or re.fullmatch(r"[A-Za-z0-9.-]+", item) is None for item in allowed):
        raise EnvironmentBuildError(ToolEnvironmentFailure.CONFIGURATION_ERROR)
    network = f"yp-deps-net-{uuid4().hex}"
    proxy = f"yp-deps-proxy-{uuid4().hex}"
    script = BASE_DIR / "sandbox" / "package_proxy.py"
    try:
        await _docker_output("network", "create", *resource_labels(config.SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS), "--internal", network)
        await _docker_output(
            "run",
            "--detach",
            "--pull=never",
            "--rm",
            "--name",
            proxy,
            *resource_labels(config.SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS),
            "--network=bridge",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--memory=128m",
            "--cpus=0.5",
            "--pids-limit=64",
            "--env",
            f"PACKAGE_ALLOWED_HOSTS={json.dumps(allowed)}",
            "--mount",
            f"type=bind,src={script},dst=/proxy.py,readonly",
            image,
            "timeout",
            "-s",
            "KILL",
            str(max(10, min(config.SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS, 1800)) + 15),
            "python",
            "-B",
            "/proxy.py",
        )
        await _docker_output("network", "connect", "--alias", "package-proxy", network, proxy)
        # 有界重试等待代理监听，避免慢速宿主机上准备容器首次连接发生竞争。
        await _docker_output(
            "exec",
            proxy,
            "python",
            "-c",
            "import socket,time\n"
            "for attempt in range(30):\n"
            " try:\n"
            "  socket.create_connection(('127.0.0.1',8765),timeout=0.1).close(); break\n"
            " except OSError:\n"
            "  if attempt == 29: raise\n"
            "  time.sleep(0.1)\n",
        )
        yield network
    finally:
        await _remove_container(proxy)
        try:
            await _docker_output("network", "rm", network)
        except (EnvironmentBuildError, OSError, TimeoutError):
            pass


async def _run_preparer(image: str, stage: Path, mounts: list[str], command: list[str], network: str | None, name: str) -> None:
    """在有界容器内下载或离线安装 wheel，退出与取消时清理容器。"""
    process = await asyncio.create_subprocess_exec(
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "--name",
        name,
        *resource_labels(get_config().SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS),
        f"--network={network}" if network else "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--memory=512m",
        "--cpus=1",
        "--pids-limit=64",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m",
        "--ulimit",
        f"fsize={MAX_ENVIRONMENT_BYTES}:{MAX_ENVIRONMENT_BYTES}",
        *mounts,
        image,
        "timeout",
        "-s",
        "KILL",
        str(max(10, min(get_config().SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS, 1800)) + 5),
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    readers = asyncio.gather(_read_bounded(process.stdout), _read_bounded(process.stderr), process.wait())
    try:
        while not readers.done():
            await asyncio.wait({readers}, timeout=0.5)
            await asyncio.to_thread(_check_build_size, stage)
        _, errors, _ = await readers
        if process.returncode:
            code = ToolEnvironmentFailure.DEPENDENCIES_UNAVAILABLE if "download" in command else ToolEnvironmentFailure.INSTALL_FAILED
            raise EnvironmentBuildError(code, _missing_dependency(errors))
    finally:
        if process.returncode is None:
            await _remove_container(name)
            if process.returncode is None:
                process.kill()
            await process.wait()
        if not readers.done():
            readers.cancel()
        await asyncio.gather(readers, return_exceptions=True)


async def _build_environment(requirements: list[str], image: str, spec_hash: str) -> tuple[str, list[dict], list[dict]]:
    """先获取 wheel，再在禁网容器中按完整哈希锁安装并保存只读缓存。"""
    config = get_config()
    root = environment_root()
    with managed_workspace(
        root, "build-", config.SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS, reserve_bytes=BUILD_RESERVATION_BYTES
    ) as directory:
        stage = Path(directory)
        wheels, site = stage / "wheels", stage / "site"
        wheels.mkdir(mode=0o777)
        site.mkdir(mode=0o777)
        os.chmod(wheels, 0o777)
        os.chmod(site, 0o777)
        request = stage / "requirements.txt"
        request.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        request.chmod(0o644)
        mounts = ["--mount", f"type=bind,src={request},dst=/requirements.txt,readonly", "--mount", f"type=bind,src={wheels},dst=/wheels"]
        command = [
            "python",
            "-m",
            "pip",
            "--isolated",
            "download",
            "--quiet",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--only-binary=:all:",
            "--retries=0",
            "--timeout=15",
            "--dest=/wheels",
            "-r",
            "/requirements.txt",
        ]
        if config.SCRIPT_DEPENDENCY_ALLOW_NETWORK:
            index = urlsplit(config.SCRIPT_DEPENDENCY_INDEX_URL)
            if (
                index.scheme != "https"
                or not index.hostname
                or index.port not in (None, 443)
                or index.username
                or index.password
                or index.fragment
            ):
                raise EnvironmentBuildError(ToolEnvironmentFailure.CONFIGURATION_ERROR)
            command.extend(["--index-url", config.SCRIPT_DEPENDENCY_INDEX_URL])
        else:
            command.append("--no-index")
        if config.SCRIPT_DEPENDENCY_WHEELHOUSE:
            offline = Path(config.SCRIPT_DEPENDENCY_WHEELHOUSE).expanduser()
            offline = (offline if offline.is_absolute() else BASE_DIR / offline).resolve()
            if not offline.is_dir():
                raise EnvironmentBuildError(ToolEnvironmentFailure.CONFIGURATION_ERROR)
            mounts.extend(["--mount", f"type=bind,src={offline},dst=/offline,readonly"])
            command.append("--find-links=/offline")
        if config.SCRIPT_DEPENDENCY_ALLOW_NETWORK:
            command.extend(["--proxy", "http://package-proxy:8765"])
            async with _package_network(image) as network:
                await _run_preparer(image, stage, mounts, command, network, f"yp-deps-{uuid4().hex}")
        else:
            await _run_preparer(image, stage, mounts, command, None, f"yp-deps-{uuid4().hex}")
        lock = []
        for wheel in sorted(wheels.iterdir()):
            if wheel.suffix != ".whl" or wheel.is_symlink():
                raise EnvironmentBuildError(ToolEnvironmentFailure.INVALID_ARTIFACT)
            name, version, _, _ = parse_wheel_filename(wheel.name)
            with wheel.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            lock.append(
                {
                    "name": str(name),
                    "version": str(version),
                    "sha256": digest,
                }
            )
        locked_request = stage / "locked.txt"
        locked_request.write_text(
            "\n".join(f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}" for item in lock) + "\n", encoding="utf-8"
        )
        locked_request.chmod(0o644)
        mounts[1] = f"type=bind,src={locked_request},dst=/requirements.txt,readonly"
        mounts.extend(["--mount", f"type=bind,src={site},dst=/site"])
        command = [
            "python",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--find-links=/wheels",
            "--only-binary=:all:",
            "--no-deps",
            "--ignore-installed",
            "--require-hashes",
            "--no-compile",
            "--target=/site",
            "-r",
            "/requirements.txt",
        ]
        await _run_preparer(image, stage, mounts, command, None, f"yp-deps-{uuid4().hex}")
        mark_resource_directory(site, config.SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS)
        manifest = await asyncio.to_thread(_file_manifest, site)
        key = hashlib.sha256(f"{spec_hash}:{uuid4().hex}".encode()).hexdigest()
        destination = root / key
        # 各构建只发布完整独立目录，已发布版本的运行环境不会被覆盖。
        site.rename(destination)
        return key, lock, manifest


async def prepare_next_tool_environment(sessions: async_sessionmaker[AsyncSession]) -> bool:
    """用数据库租约领取一个当前版本任务，支持崩溃恢复、缓存复用和取消清理。"""
    config = get_config()
    timeout = max(10, min(config.SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS, 1800))
    now = shanghai_now(naive=True)
    async with sessions.begin() as db:
        await db.execute(
            update(UserToolVersion)
            .where(
                UserToolVersion.environment_state == ToolEnvironmentState.BUILDING,
                UserToolVersion.environment_lease_expires_at <= now,
            )
            .values(
                environment_state=ToolEnvironmentState.FAILED,
                environment_error_code=ToolEnvironmentFailure.WORKER_INTERRUPTED,
                environment_lease_token=None,
                environment_lease_expires_at=None,
            )
        )
        version = await db.scalar(
            select(UserToolVersion)
            .join(UserTool, UserTool.id == UserToolVersion.tool_id)
            .where(
                UserTool.is_deleted == 0,
                UserTool.current_version == UserToolVersion.version,
                UserToolVersion.environment_state == ToolEnvironmentState.PENDING,
            )
            .order_by(UserToolVersion.id)
            .limit(1)
        )
        if version is None:
            return False
        try:
            owner_id = await db.scalar(select(UserTool.owner_id).where(UserTool.id == version.tool_id))
            slot_token = await acquire_tool_slot(db, owner_id, None, ToolLeaseKind.PREPARATION, timeout)
        except ToolCapacityError:
            return False
        token = str(uuid4())
        claimed = await db.execute(
            update(UserToolVersion)
            .where(
                UserToolVersion.id == version.id,
                UserToolVersion.environment_state == ToolEnvironmentState.PENDING,
            )
            .values(
                environment_state=ToolEnvironmentState.BUILDING,
                environment_lease_token=token,
                environment_lease_expires_at=now + timedelta(seconds=timeout + 30),
                environment_attempts=UserToolVersion.environment_attempts + 1,
            )
        )
        if claimed.rowcount != 1:
            from app.models.tool_execution import ToolExecutionLease
            from sqlalchemy import delete

            await db.execute(delete(ToolExecutionLease).where(ToolExecutionLease.token == slot_token))
            return False
        version_id, requirements = version.id, list(version.requirements)
    result = {}
    try:
        async with asyncio.timeout(timeout):
            metadata = json.loads(await _docker_output("image", "inspect", config.SCRIPT_SANDBOX_IMAGE))
            image = metadata[0]["Id"]
            identity = {
                "requirements": requirements,
                "image": image,
                "architecture": metadata[0]["Architecture"],
                "os": metadata[0]["Os"],
            }
            spec_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            async with sessions() as db:
                cached = await db.scalar(
                    select(UserToolVersion)
                    .where(
                        UserToolVersion.environment_state == ToolEnvironmentState.READY,
                        UserToolVersion.environment_spec_hash == spec_hash,
                    )
                    .order_by(UserToolVersion.id.desc())
                    .limit(1)
                )
            if cached is not None:
                try:
                    await asyncio.to_thread(_verify_environment, cached.environment_key, cached.environment_manifest)
                except (OSError, EnvironmentBuildError):
                    cached = None
            if cached is not None:
                key, lock, manifest = cached.environment_key, cached.environment_lock, cached.environment_manifest
            else:
                key, lock, manifest = await _build_environment(requirements, image, spec_hash)
            result = {
                "environment_state": ToolEnvironmentState.READY,
                "environment_image": image,
                "environment_key": key,
                "environment_spec_hash": spec_hash,
                "environment_lock": lock,
                "environment_manifest": manifest,
                "environment_error_code": None,
                "environment_error_detail": None,
            }
    except asyncio.CancelledError:
        result = {
            "environment_state": ToolEnvironmentState.FAILED,
            "environment_error_code": ToolEnvironmentFailure.WORKER_INTERRUPTED,
        }
        raise
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, EnvironmentBuildError)
            else (
                ToolEnvironmentFailure.SIZE_LIMIT
                if isinstance(exc, ToolStorageCapacityError)
                else ToolEnvironmentFailure.BUILD_TIMEOUT
                if isinstance(exc, TimeoutError)
                else ToolEnvironmentFailure.CONFIGURATION_ERROR
            )
        )
        result = {
            "environment_state": ToolEnvironmentState.FAILED,
            "environment_error_code": code,
            "environment_error_detail": exc.detail if isinstance(exc, EnvironmentBuildError) else None,
        }
    finally:
        async with sessions.begin() as db:
            await db.execute(
                update(UserToolVersion)
                .where(
                    UserToolVersion.id == version_id,
                    UserToolVersion.environment_lease_token == token,
                    UserToolVersion.environment_state == ToolEnvironmentState.BUILDING,
                )
                .values(**result, environment_lease_token=None, environment_lease_expires_at=None)
            )
        await release_tool_slot(sessions, slot_token)
    return True
