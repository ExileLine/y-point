# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 22:33:20
# @Author  : yangyuexiong
# @File    : test_tool_environments.py

"""验证用户依赖声明、离线 wheel 准备、版本缓存、发布和执行隔离。"""

import asyncio
import base64
import csv
import hashlib
import io
import importlib.util
import http.client
import json
import os
import sqlite3
import shutil
import subprocess
import sys
import zipfile
from datetime import timedelta
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.enums import ToolEnvironmentFailure, ToolEnvironmentState
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.base import Base, shanghai_now
from app.models.tool import AgentTool, ToolInvocation, UserTool, UserToolVersion
from app.services import tool_environments
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run
from app.services.user_tools import snapshot_run_tools
from app.services.worker_tools import make_worker_tool_resolver
from app.services.script_sandbox import SandboxConfigurationError, run_script
from app.services.tool_environments import normalize_requirements, prepare_next_tool_environment, require_script_environment


class DependencyModel(FakeMessagesListChatModel):
    """让真实运行图按指定序列调用有依赖的脚本。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """接收正式工具注册，不依赖外部模型服务。"""
        return self


async def _execute_dependency_run(sessions, tool_id: int, tmp_path: Path) -> None:
    """验证正式 worker 根据固定 Tool 版本加载依赖并留下调用审计。"""
    async with sessions.begin() as db:
        db.add(AgentConfig(id=3, owner_id=1, name="Dependency Agent", default_model_id=1, enabled=True, version=1))
        db.add(AgentTool(agent_id=3, tool_id=tool_id, tool_version=1))
        await db.flush()
        run = await create_run(db, 1, agent_id=3, input_text="调用 dependency_demo")
        await snapshot_run_tools(db, run.id, 3, 1)
    model = DependencyModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "dependency_demo",
                        "args": {},
                        "id": "dependency-call",
                    }
                ],
            ),
            AIMessage(content="42"),
        ]
    )
    result = await execute_text_run(sessions, run.id, model, tmp_path / "checkpoints.db", resolve_tools=make_worker_tool_resolver(sessions))
    assert result.state == "completed", result.failure_code
    async with sessions() as db:
        invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
        assert invocation.state == "completed" and invocation.tool_version == 1


def _wheel(root: Path, name: str, version: str, source: str, dependencies: list[str] | None = None) -> None:
    """生成不含构建脚本的真实 wheel，供完全离线的 Docker 回归使用。"""
    distribution = name.replace("-", "_")
    info = f"{distribution}-{version}.dist-info"
    files = {
        f"{distribution}.py": source.encode(),
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + "".join(f"Requires-Dist: {item}\n" for item in dependencies or [])
            + "\n"
        ).encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: y-point-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = io.StringIO()
    writer = csv.writer(record)
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        writer.writerow([path, f"sha256={digest}", len(content)])
    writer.writerow([f"{info}/RECORD", "", ""])
    files[f"{info}/RECORD"] = record.getvalue().encode()
    root.mkdir(exist_ok=True)
    with zipfile.ZipFile(root / f"{distribution}-{version}-py3-none-any.whl", "w") as archive:
        for path, content in files.items():
            archive.writestr(path, content)


def _offline_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """将包源与缓存隔离到测试目录，并禁止准备容器联网。"""
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    config = get_config()
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_ALLOW_NETWORK", False)
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_WHEELHOUSE", str(wheels))
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_STORAGE_DIR", str(tmp_path / "environments"))
    return wheels


def test_requirements_accept_standard_constraints_and_reject_external_sources() -> None:
    """允许标准包约束，拒绝 URL、pip 参数、路径和多行注入。"""
    assert normalize_requirements(["HTTPX >=0.27,<1", "httpx<1,>=0.27"]) == ["httpx<1,>=0.27"]
    for value in ["-r /etc/passwd", "demo @ https://example.com/a.whl", "./package", "httpx\n--extra-index-url=https://example.com"]:
        with pytest.raises(ValueError):
            normalize_requirements([value])


@pytest.fixture
def sandbox_image() -> None:
    """仅真实容器集成测试需要已启动的 Docker 及预构建沙箱镜像。"""
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", get_config().SCRIPT_SANDBOX_IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("Docker sandbox image is unavailable")
    if result.returncode:
        pytest.skip("Build Dockerfile.sandbox before running container integration tests")


def test_dependency_api_offline_prepare_test_cache_and_versioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_image: None
) -> None:
    """贯通创建、禁网准备、依赖锁、试运行、发布、缓存复用和依赖变更。"""
    wheels = _offline_config(monkeypatch, tmp_path)
    _wheel(wheels, "yp-leaf", "2.0.0", "VALUE = 42\n")
    _wheel(wheels, "yp-demo", "1.0.0", "from yp_leaf import VALUE\n", ["yp-leaf>=2,<3"])

    async def exercise() -> None:
        """使用隔离数据库和真实 Docker，只从本地 wheel 获取依赖。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'environment-api.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="env-owner", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                        Admin(id=2, username="env-other", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                    ]
                )

            async def session_override():
                """让公开接口使用隔离业务库。"""
                async with sessions() as db:
                    yield db

            app.dependency_overrides[get_db_session] = session_override
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:

                async def login(username: str) -> dict:
                    """登录并返回测试用户的认证头。"""
                    response = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "identifier": username,
                            "password": "StrongPassword123",
                        },
                    )
                    return {
                        "Authorization": f"Bearer {response.json()['data']['access_token']}",
                    }

                owner, other = await login("env-owner"), await login("env-other")
                payload = {
                    "name": "dependency_demo",
                    "description": "从自定义依赖中读取值",
                    "kind": "script",
                    "script_language": "python",
                    "input_schema": {
                        "type": "object",
                        "additionalProperties": False,
                    },
                    "source_code": "import json, yp_demo\nprint(json.dumps({'value': yp_demo.VALUE}))\n",
                    "requirements": ["yp-demo>=1,<2"],
                }
                created = await client.post("/api/v1/tools", headers=owner, json=payload)
                assert created.status_code == 201, created.text
                tool_id = created.json()["data"]["id"]
                assert created.json()["data"]["environment"]["state"] == "pending"
                tested = await client.post(
                    f"/api/v1/tools/{tool_id}/test",
                    headers=owner,
                    json={
                        "arguments": {},
                    },
                )
                assert tested.status_code == 409
                assert await prepare_next_tool_environment(sessions)
                full = (await client.get(f"/api/v1/tools/{tool_id}", headers=owner)).json()["data"]
                assert full["environment"]["state"] == "ready", full
                assert {(item["name"], item["version"]) for item in full["environment"]["locked_requirements"]} == {
                    ("yp-demo", "1.0.0"),
                    ("yp-leaf", "2.0.0"),
                }
                tested = await client.post(
                    f"/api/v1/tools/{tool_id}/test",
                    headers=owner,
                    json={
                        "arguments": {},
                    },
                )
                assert tested.status_code == 200, tested.text
                assert json.loads(tested.json()["data"]["result"]) == {
                    "value": 42,
                }
                await _execute_dependency_run(sessions, tool_id, tmp_path)
                published = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    headers=owner,
                    json={
                        "expected_version": 1,
                        "visibility": "public",
                    },
                )
                assert published.status_code == 200, published.text
                assert published.json()["data"]["environment"]["state"] == "ready"
                public = (await client.get(f"/api/v1/tools/{tool_id}", headers=other)).json()["data"]
                assert "requirements" not in public and "environment" not in public

                async def unexpected_build(*args, **kwargs):
                    """相同依赖已有有效环境时不应再次执行准备容器。"""
                    raise AssertionError("expected cache reuse")

                with monkeypatch.context() as patch:
                    patch.setattr(tool_environments, "_build_environment", unexpected_build)
                    copied = await client.post(
                        "/api/v1/tools",
                        headers=owner,
                        json={
                            **payload,
                            "name": "cached_demo",
                        },
                    )
                    assert copied.status_code == 201
                    assert await prepare_next_tool_environment(sessions)
                    cached_id = copied.json()["data"]["id"]
                    cached = (await client.get(f"/api/v1/tools/{cached_id}", headers=owner)).json()["data"]
                    assert cached["environment"]["state"] == "ready", cached
                changed = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    headers=owner,
                    json={
                        "expected_version": 2,
                        "requirements": ["yp-demo==9.0.0"],
                    },
                )
                assert changed.status_code == 200, changed.text
                assert changed.json()["data"]["visibility"] == "private"
                assert changed.json()["data"]["verified"] is False
                assert changed.json()["data"]["environment"]["state"] == "pending"
                assert await prepare_next_tool_environment(sessions)
                failed = (await client.get(f"/api/v1/tools/{tool_id}", headers=owner)).json()["data"]
                assert failed["environment"]["state"] == "failed"
                assert failed["environment"]["failure_code"] == "dependencies_unavailable"
                assert "yp-demo==9.0.0" in failed["environment"]["message"]
                denied = await client.post(
                    f"/api/v1/tools/{tool_id}/environment/retry",
                    headers=other,
                    json={
                        "expected_version": 3,
                    },
                )
                assert denied.status_code == 404
                stale = await client.post(
                    f"/api/v1/tools/{tool_id}/environment/retry",
                    headers=owner,
                    json={
                        "expected_version": 2,
                    },
                )
                assert stale.status_code == 409
                _wheel(wheels, "yp-demo", "9.0.0", "VALUE = 90\n")
                retried = await client.post(
                    f"/api/v1/tools/{tool_id}/environment/retry",
                    headers=owner,
                    json={
                        "expected_version": 3,
                    },
                )
                assert retried.status_code == 200
                assert retried.json()["data"]["environment"]["state"] == "pending"
                assert await prepare_next_tool_environment(sessions)
                tested = await client.post(
                    f"/api/v1/tools/{tool_id}/test",
                    headers=owner,
                    json={
                        "arguments": {},
                    },
                )
                assert tested.status_code == 200, tested.text
                assert json.loads(tested.json()["data"]["result"])["value"] == 90
                async with sessions() as db:
                    old = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == 1))
                    current = await db.scalar(
                        select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == 3)
                    )
                    assert old.environment_lock != current.environment_lock
                    environment = await require_script_environment(old)
                    assert json.loads(await run_script("python", payload["source_code"], {}, 10, environment=environment))["value"] == 42
                    with pytest.raises(ValueError, match="脚本执行失败"):
                        await run_script(
                            "python", "import socket; socket.create_connection(('1.1.1.1',80),timeout=1)", {}, 10, environment=environment
                        )
                    (environment.site / "yp_demo.py").write_text("VALUE = 123", encoding="utf-8")
                    with pytest.raises(SandboxConfigurationError, match="校验失败"):
                        await require_script_environment(old)
                    current_environment = await require_script_environment(current)
                    old_key = current.environment_key
                    old_lock = list(current.environment_lock)
                    (current_environment.site / "yp_demo.py").write_text("VALUE = 999", encoding="utf-8")
                broken = await client.post(
                    f"/api/v1/tools/{tool_id}/test",
                    headers=owner,
                    json={
                        "arguments": {},
                    },
                )
                assert broken.status_code == 400
                assert "校验失败" in broken.json()["message"]
                repaired = await client.post(
                    f"/api/v1/tools/{tool_id}/environment/retry",
                    headers=owner,
                    json={
                        "expected_version": 3,
                    },
                )
                assert repaired.status_code == 200, repaired.text
                assert repaired.json()["data"]["current_version"] == 4
                assert repaired.json()["data"]["verified"] is False
                assert repaired.json()["data"]["enabled"] is False
                assert repaired.json()["data"]["environment"]["state"] == "pending"
                assert await prepare_next_tool_environment(sessions)
                async with sessions() as db:
                    previous = await db.scalar(
                        select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == 3)
                    )
                    fixed = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == 4))
                    assert previous.environment_key == old_key
                    assert previous.environment_lock == old_lock
                    assert fixed.environment_key != old_key
                    assert fixed.environment_state == ToolEnvironmentState.READY
                    await require_script_environment(fixed)
                retested = await client.post(
                    f"/api/v1/tools/{tool_id}/test",
                    headers=owner,
                    json={
                        "arguments": {},
                    },
                )
                assert retested.status_code == 200, retested.text
                assert json.loads(retested.json()["data"]["result"])["value"] == 90
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            await engine.dispose()

    asyncio.run(exercise())


def test_dependency_job_lease_and_cancellation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发 worker 不能重复领取同一版本，退出后状态可恢复并可重试。"""
    _offline_config(monkeypatch, tmp_path)

    async def exercise() -> None:
        """在首个构建暂停期间验证领取互斥及取消后的持久化失败。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'environment-lease.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        started, release = asyncio.Event(), asyncio.Event()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(
                    UserTool(
                        id=1,
                        owner_id=1,
                        name="lease_demo",
                        description="租约测试",
                        kind="script",
                        visibility="private",
                        enabled=False,
                        current_version=1,
                    )
                )
                db.add(
                    UserToolVersion(
                        tool_id=1,
                        version=1,
                        content_hash="a" * 64,
                        input_schema={},
                        effect="read",
                        requires_approval=False,
                        verified=False,
                        requirements=["yp-demo"],
                        environment_state="pending",
                        share_credentials=False,
                    )
                )

            async def blocked_build(*args, **kwargs):
                """暂停实际下载，让另一个 worker 尝试领取同一任务。"""
                started.set()
                await release.wait()
                raise AssertionError("cancelled build must not complete")

            async def image_metadata(*args):
                """租约测试固定镜像元信息，不依赖真实 Docker 服务。"""
                return json.dumps(
                    [
                        {
                            "Id": "sha256:" + "a" * 64,
                            "Architecture": "amd64",
                            "Os": "linux",
                        }
                    ]
                ).encode()

            monkeypatch.setattr(tool_environments, "_docker_output", image_metadata)
            monkeypatch.setattr(tool_environments, "_build_environment", blocked_build)
            task = asyncio.create_task(prepare_next_tool_environment(sessions))
            await asyncio.wait_for(started.wait(), timeout=10)
            assert await prepare_next_tool_environment(sessions) is False
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with sessions.begin() as db:
                version = await db.scalar(select(UserToolVersion))
                assert version.environment_state == ToolEnvironmentState.FAILED
                assert version.environment_error_code == ToolEnvironmentFailure.WORKER_INTERRUPTED
                assert version.environment_lease_token is None
                version.environment_state = ToolEnvironmentState.BUILDING
                version.environment_lease_token = "old-token"
                version.environment_lease_expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            assert await prepare_next_tool_environment(sessions) is False
            async with sessions() as db:
                version = await db.scalar(select(UserToolVersion))
                assert version.environment_state == ToolEnvironmentState.FAILED
                assert version.environment_lease_token is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_environment_migration_preserves_existing_versions(tmp_path: Path) -> None:
    """升级回填依赖状态，迁移往返不改变历史 Tool 摘要或验证结果。"""
    database = tmp_path / "environment-migration.db"
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        }
    )

    def migrate(*arguments: str) -> None:
        """仅对隔离库执行迁移并公开失败断言。"""
        result = subprocess.run([sys.executable, "-m", "alembic", *arguments], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "039")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO yp_user_tool_version (tool_id,version,content_hash,input_schema,effect,requires_approval,verified,"
            "share_credentials,"
            "create_time,update_time,create_timestamp,update_timestamp,is_deleted,status) "
            "VALUES (6,1,?,'{}','read',0,1,0,'2026-11-02','2026-11-02',0,0,0,1)",
            ("a" * 64,),
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT content_hash,verified,requirements,environment_state FROM yp_user_tool_version").fetchone()
        assert row == ("a" * 64, 1, "[]", "not_required")
    migrate("downgrade", "039")
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT content_hash,verified FROM yp_user_tool_version").fetchone()
        assert row == ("a" * 64, 1)
    migrate("upgrade", "head")
    migrate("check")


def test_package_proxy_rejects_unapproved_destinations(monkeypatch: pytest.MonkeyPatch) -> None:
    """实际 HTTP CONNECT 请求必须在联网前拒绝其他域名、IP 和端口。"""
    monkeypatch.setenv("PACKAGE_ALLOWED_HOSTS", '["pypi.org"]')
    path = Path(__file__).resolve().parents[1] / "sandbox" / "package_proxy.py"
    spec = importlib.util.spec_from_file_location("yp_test_package_proxy", path)
    proxy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proxy)

    def forbidden_connection(*args, **kwargs):
        """拒绝规则不能尝试连接远端目标。"""
        raise AssertionError("Unexpected outbound connection")

    monkeypatch.setattr(proxy, "socket", SimpleNamespace(create_connection=forbidden_connection))
    server = proxy.ThreadingHTTPServer(("127.0.0.1", 0), proxy.PackageProxy)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for target in ["example.com:443", "127.0.0.1:443", "pypi.org:80", "pypi.org:8443", "pypi.org.attacker.example:443"]:
            connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request("CONNECT", target)
                response = connection.getresponse()
                assert response.status == 403
                response.read()
            finally:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_preparation_cancellation_removes_containers_and_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandbox_image: None
) -> None:
    """真实准备任务取消后应清理安装容器、包代理和内部 Docker 网络。"""
    config = get_config()
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS", 60)

    async def exercise() -> None:
        """等待容器确实启动后取消，验证清理行为而非仅取消排队协程。"""
        image = json.loads(await tool_environments._docker_output("image", "inspect", config.SCRIPT_SANDBOX_IMAGE))[0]["Id"]
        name = f"yp-deps-test-{uuid4().hex}"
        networks = []

        async def prepare() -> None:
            """复用正式代理和准备容器，运行可被取消的受控等待。"""
            async with tool_environments._package_network(image) as network:
                networks.append(network)
                await tool_environments._run_preparer(image, tmp_path, [], ["python", "-c", "import time; time.sleep(60)"], network, name)

        task = asyncio.create_task(prepare())
        try:
            async with asyncio.timeout(15):
                while True:
                    if task.done():
                        await task
                        raise AssertionError("Preparation unexpectedly completed")
                    try:
                        metadata = json.loads(await tool_environments._docker_output("container", "inspect", name))
                    except tool_environments.EnvironmentBuildError:
                        await asyncio.sleep(0.1)
                        continue
                    if metadata[0]["State"]["Running"]:
                        break
                    await asyncio.sleep(0.1)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(networks) == 1
        with pytest.raises(tool_environments.EnvironmentBuildError):
            await tool_environments._docker_output("container", "inspect", name)
        with pytest.raises(tool_environments.EnvironmentBuildError):
            await tool_environments._docker_output("network", "inspect", networks[0])

    asyncio.run(exercise())
