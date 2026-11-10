# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:21:42
# @Author  : yangyuexiong
# @File    : test_m51_governance.py

"""验证 M5.1 凭据、接收预算、跨进程并发、存储预留及故障回收。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.enums import RecoveryPolicy, ToolEffect, ToolKind, ToolLeaseKind, UserRole, Visibility
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import Base, shanghai_now
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.models.tool_execution import ToolExecutionLease
from app.services.mcp_tools import _PinnedPublicTransport, _discover_session
from app.services.run_manager import claim_run, create_approval_request, create_run, decide_approval
from app.services.tool_execution import ToolCapacityError, acquire_tool_slot, release_tool_slot, tool_execution_slot
from app.services.tool_resources import (
    BUILD_RESERVATION_BYTES,
    ToolStorageCapacityError,
    managed_workspace,
    mark_resource_directory,
    prune_tool_resources,
    resource_scope,
    storage_status,
)
from app.services.user_tools import load_run_tools
from app.services.run_executor import execute_text_run
from app.services.worker_tools import make_worker_tool_resolver
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


@asynccontextmanager
async def _database(tmp_path: Path):
    """为每个治理场景建立独立业务库并关闭连接。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'governance.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        await engine.dispose()


@asynccontextmanager
async def _http_platform(sessions):
    """通过真实认证路由验证两个用户的 Tool 管理权限。"""
    async with sessions.begin() as db:
        for index, username in enumerate(("governance-a", "governance-b"), 1):
            db.add(Admin(id=index, username=username, password=hash_password("StrongPassword123"), role=UserRole.USER, status=1, is_deleted=0))

    async def override_db():
        """向 HTTP 路由提供隔离数据库。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = override_db
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = []
            for username in ("governance-a", "governance-b"):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": username,
                        "password": "StrongPassword123",
                    },
                )
                assert response.status_code == 200
                headers.append(
                    {
                        "Authorization": f"Bearer {response.json()['data']['access_token']}",
                    },
                )
            yield client, headers
    finally:
        app.dependency_overrides.clear()


def test_mcp_transport_limits_before_sdk_parsing(monkeypatch) -> None:
    """声明长度、压缩编码及分块超限均关闭原始流，后续请求共享预算。"""
    monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 8)

    class Stream(httpx.AsyncByteStream):
        """提供可观测的分块字节流。"""

        def __init__(self, chunks):
            """保存待发送分块及关闭标记。"""
            self.chunks = chunks
            self.closed = False

        async def __aiter__(self):
            """逐块交付，模拟无 Content-Length 的 SSE 或 JSON。"""
            for chunk in self.chunks:
                yield chunk

        async def aclose(self):
            """记录传输层是否及时关闭响应。"""
            self.closed = True

    async def exercise():
        """分别验证解析前拒绝和连接累计预算。"""
        for headers, chunks in (
            (
                {
                    "Content-Length": "9",
                },
                [b"secret"],
            ),
            (
                {
                    "Content-Encoding": "gzip",
                },
                [b"secret"],
            ),
            ({}, [b"1234", b"56789"]),
        ):
            stream = Stream(chunks)

            async def respond(request):
                """构造恶意上游响应。"""
                return httpx.Response(200, headers=headers, stream=stream)

            transport = _PinnedPublicTransport("mcp.example.com", "1.1.1.1")
            await transport.transport.aclose()
            transport.transport = httpx.MockTransport(respond)
            async with httpx.AsyncClient(transport=transport) as client:
                with pytest.raises(ValueError):
                    await client.get("https://mcp.example.com/mcp")
            assert stream.closed
        transport = _PinnedPublicTransport("mcp.example.com", "1.1.1.1")
        await transport.transport.aclose()
        transport.transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream([b"12345"])))
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get("https://mcp.example.com/mcp")).content == b"12345"
            with pytest.raises(ValueError, match="字节上限"):
                await client.get("https://mcp.example.com/mcp")

    asyncio.run(exercise())


def test_mcp_discovery_rejects_unbounded_pages_and_schema(monkeypatch) -> None:
    """发现数量、描述、Schema、结果总量和循环游标分别受到限制。"""
    config = get_config()

    def tool(name="lookup", description="查找", schema=None):
        """构造协议已解析但仍需业务限额校验的工具。"""
        return SimpleNamespace(
            name=name,
            description=description,
            inputSchema=schema
            or {
                "type": "object",
            },
            annotations=None,
        )

    class Session:
        """按游标提供固定发现页。"""

        def __init__(self, pages):
            """记录发现页及读取次数。"""
            self.pages = iter(pages)

        async def list_tools(self, cursor=None):
            """返回下一页发现结果。"""
            return next(self.pages)

    async def exercise():
        """用相互独立的超限输入核对每个边界。"""
        monkeypatch.setattr(config, "MCP_DISCOVERY_MAX_TOOLS", 1)
        page = SimpleNamespace(tools=[tool("a"), tool("b")], nextCursor=None)
        with pytest.raises(ValueError, match="数量"):
            await _discover_session(Session([page]))
        for candidate in (
            tool(description="中" * 1400),
            tool(
                schema={
                    "type": "object",
                    "description": "x" * 16384,
                },
            ),
        ):
            with pytest.raises(ValueError, match="超限"):
                await _discover_session(Session([SimpleNamespace(tools=[candidate], nextCursor=None)]))
        monkeypatch.setattr(config, "MCP_DISCOVERY_MAX_BYTES", 32)
        with pytest.raises(ValueError, match="总字节"):
            await _discover_session(Session([SimpleNamespace(tools=[tool()], nextCursor=None)]))
        monkeypatch.setattr(config, "MCP_DISCOVERY_MAX_BYTES", 4096)
        page = SimpleNamespace(tools=[], nextCursor="same")
        with pytest.raises(ValueError, match="游标重复"):
            await _discover_session(Session([page, page]))

    asyncio.run(exercise())


def test_mcp_credentials_rotate_clear_revoke_and_cancel_old_runs(tmp_path, monkeypatch) -> None:
    """轮换及清除撤销历史凭据，远端失联时仍可撤销，跨用户不可管理密钥。"""
    tokens = []
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "1.1.1.1")

    async def discover(url, token):
        """模拟有凭据和匿名发现，记录实际使用的密钥。"""
        tokens.append(token)
        return [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                },
                "read_only": True,
            },
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def exercise():
        """通过 HTTP 管理凭据，再用固定 Run 版本核对撤销语义。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            payload = {
                "name": "credential_lookup",
                "description": "查询信息",
                "kind": "mcp",
                "visibility": "public",
                "mcp_url": "https://mcp.example.com/mcp",
                "mcp_tool_name": "lookup",
                "mcp_bearer_token": "first-private-secret",
            }
            assert (await client.post("/api/v1/tools", headers=owner, json=payload)).status_code == 400
            created = await client.post(
                "/api/v1/tools",
                headers=owner,
                json={
                    **payload,
                    "share_credentials": True,
                },
            )
            assert created.status_code == 201, created.text
            data = created.json()["data"]
            tool_id = data["id"]
            assert data["effect"] == "read" and "first-private-secret" not in created.text
            async with sessions.begin() as db:
                run = await create_run(db, 2, input_text="use lookup")
                db.add(RunTool(run_id=run.id, tool_id=tool_id, tool_version=1, content_hash=data["content_hash"]))
            assert (
                await client.post(
                    f"/api/v1/tools/{tool_id}/credentials/revoke",
                    headers=other,
                    json={
                        "expected_version": 1,
                    },
                )
            ).status_code == 404
            rotated = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 1,
                    "mcp_bearer_token": "second-private-secret",
                },
            )
            assert rotated.status_code == 200, rotated.text
            assert rotated.json()["data"]["credentials_revoked_through"] == 1
            async with sessions() as db:
                assert (await db.get(AgentRun, run.id)).state == "cancelled"
                with pytest.raises(Exception, match="授权或版本"):
                    await load_run_tools(db, run.id, 2)
            conflict = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 2,
                    "mcp_bearer_token": "other",
                    "clear_mcp_credentials": True,
                },
            )
            assert conflict.status_code == 400
            cleared = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 2,
                    "clear_mcp_credentials": True,
                },
            )
            assert cleared.status_code == 200, cleared.text
            assert cleared.json()["data"]["has_credentials"] is False and tokens[-1] is None
            assert (
                await client.put(
                    f"/api/v1/tools/{tool_id}",
                    headers=owner,
                    json={
                        "expected_version": 3,
                        "mcp_bearer_token": "third-private-secret",
                    },
                )
            ).status_code == 400
            restored = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 3,
                    "mcp_bearer_token": "third-private-secret",
                    "share_credentials": True,
                },
            )
            assert restored.status_code == 200, restored.text

            async def unavailable(*args):
                """远端离线时禁止重新发现。"""
                raise OSError("unreachable")

            monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", unavailable)
            revoked = await client.post(
                f"/api/v1/tools/{tool_id}/credentials/revoke",
                headers=owner,
                json={
                    "expected_version": 4,
                },
            )
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["data"]["credentials_revoked_through"] == 4
            assert (
                await client.put(
                    f"/api/v1/tools/{tool_id}",
                    headers=owner,
                    json={
                        "expected_version": 4,
                        "enabled": True,
                    },
                )
            ).status_code == 409
            assert (
                await client.post(
                    f"/api/v1/tools/{tool_id}/credentials/revoke",
                    headers=owner,
                    json={
                        "expected_version": 1,
                    },
                )
            ).status_code == 409
            details = (await client.get(f"/api/v1/tools/{tool_id}", headers=owner)).json()["data"]
            assert details["enabled"] is False
            assert all(secret not in json.dumps(details) for secret in ("first-private-secret", "second-private-secret", "third-private-secret"))

    asyncio.run(exercise())


def test_tool_slots_enforce_each_scope_and_release_on_cancel(tmp_path, monkeypatch) -> None:
    """全局、用户、Run 与准备任务分别限额，异常和取消立即释放。"""
    config = get_config()
    for name, value in (
        ("TOOL_CALL_GLOBAL_LIMIT", 3),
        ("TOOL_CALL_USER_LIMIT", 2),
        ("TOOL_CALL_RUN_LIMIT", 1),
        ("TOOL_PREPARATION_GLOBAL_LIMIT", 1),
    ):
        monkeypatch.setattr(config, name, value)

    async def exercise():
        """逐一占用独立作用域并验证取消后可重新领取。"""
        async with _database(tmp_path) as sessions:
            async with sessions.begin() as db:
                first = await acquire_tool_slot(db, 1, "run-a", ToolLeaseKind.CALL, 10)
            for owner, run, blocked in (
                (1, "run-a", True),
                (1, "run-b", False),
                (1, "run-c", True),
                (2, "run-c", False),
                (3, "run-d", True),
            ):
                if blocked:
                    with pytest.raises(ToolCapacityError):
                        async with sessions.begin() as db:
                            await acquire_tool_slot(db, owner, run, ToolLeaseKind.CALL, 10)
                else:
                    async with sessions.begin() as db:
                        await acquire_tool_slot(db, owner, run, ToolLeaseKind.CALL, 10)
            async with sessions.begin() as db:
                preparation = await acquire_tool_slot(db, 1, None, ToolLeaseKind.PREPARATION, 10)
            with pytest.raises(ToolCapacityError):
                async with sessions.begin() as db:
                    await acquire_tool_slot(db, 2, None, ToolLeaseKind.PREPARATION, 10)
            await release_tool_slot(sessions, first)
            await release_tool_slot(sessions, preparation)
            entered = asyncio.Event()

            async def waiting_call():
                """模拟在已占用名额内被取消的工具。"""
                async with tool_execution_slot(sessions, 3, None, ToolLeaseKind.CALL, 10):
                    entered.set()
                    await asyncio.Event().wait()

            task = asyncio.create_task(waiting_call())
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease).where(ToolExecutionLease.owner_id == 3)) == 0
            async with tool_execution_slot(sessions, 3, None, ToolLeaseKind.CALL, 10):
                pass

    asyncio.run(exercise())


def test_multiple_processes_do_not_oversell_and_expired_slot_is_reclaimed(tmp_path) -> None:
    """两个独立进程争抢最后名额，进程异常退出后的租约到期可再次领取。"""
    script = """
import asyncio, sys
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.services.tool_execution import acquire_tool_slot, ToolCapacityError
from app.core.enums import ToolLeaseKind
async def exercise():
    '''在独立进程中争抢数据库工具名额并报告结果。'''
    engine = create_async_engine(sys.argv[1])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions.begin() as db:
            await acquire_tool_slot(db, 1, 'same-run', ToolLeaseKind.CALL, 10)
        print('CLAIMED', flush=True)
    except ToolCapacityError:
        print('REJECTED', flush=True)
    finally:
        await engine.dispose()
asyncio.run(exercise())
"""

    async def exercise():
        """建立数据库后让独立解释器竞争，模拟无正常释放的退出。"""
        async with _database(tmp_path) as sessions:
            url = f"sqlite+aiosqlite:///{tmp_path / 'governance.db'}"
            env = {
                **os.environ,
                "TOOL_CALL_GLOBAL_LIMIT": "1",
                "TOOL_CALL_USER_LIMIT": "1",
                "TOOL_CALL_RUN_LIMIT": "1",
            }
            processes = [
                subprocess.Popen([sys.executable, "-c", script, url], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)
            ]
            results = [await asyncio.to_thread(process.communicate, timeout=30) for process in processes]
            assert all(process.returncode == 0 for process in processes), results
            assert sorted(output.strip().splitlines()[-1] for output, _ in results) == ["CLAIMED", "REJECTED"]
            async with sessions.begin() as db:
                await db.execute(update(ToolExecutionLease).values(expires_at=shanghai_now(naive=True) - timedelta(seconds=1)))
            process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, url, env=env, stdout=asyncio.subprocess.PIPE)
            output, _ = await asyncio.wait_for(process.communicate(), timeout=30)
            assert process.returncode == 0 and output.strip().endswith(b"CLAIMED")

    asyncio.run(exercise())


def test_storage_reservations_refuse_overcommit(tmp_path, monkeypatch) -> None:
    """构建启动前按最坏空间预留，预留已满和物理余量不足均拒绝新任务。"""
    config = get_config()
    root = tmp_path / "environments"
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_STORAGE_DIR", str(root))
    monkeypatch.setattr(config, "TOOL_STORAGE_MAX_BYTES", BUILD_RESERVATION_BYTES + 4096)
    monkeypatch.setattr(config, "TOOL_STORAGE_MIN_FREE_BYTES", 0)
    with managed_workspace(root, "build-", 10, reserve_bytes=BUILD_RESERVATION_BYTES):
        assert storage_status()["accounted_bytes"] >= BUILD_RESERVATION_BYTES
        with pytest.raises(ToolStorageCapacityError):
            with managed_workspace(root, "build-", 10, reserve_bytes=BUILD_RESERVATION_BYTES):
                pytest.fail("超额预留不应成功")
    assert storage_status()["accounted_bytes"] < 4096
    monkeypatch.setattr(config, "TOOL_STORAGE_MIN_FREE_BYTES", 10**18)
    with pytest.raises(ToolStorageCapacityError):
        with managed_workspace(root, "build-", 10, reserve_bytes=BUILD_RESERVATION_BYTES):
            pytest.fail("物理空间不足不应成功")


def test_expired_docker_resources_and_workspaces_are_reconciled(tmp_path, monkeypatch) -> None:
    """真实 Docker 过期容器与网络被回收，其他部署和历史版本环境得到保留。"""
    if subprocess.run(["docker", "image", "inspect", get_config().SCRIPT_SANDBOX_IMAGE], capture_output=True).returncode:
        pytest.skip("需要已准备的沙箱镜像")
    config = get_config()
    root = tmp_path / "resources"
    root.mkdir()
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_STORAGE_DIR", str(root))
    monkeypatch.setattr(config, "TOOL_STORAGE_MIN_FREE_BYTES", 0)
    scope = resource_scope()
    suffix = uuid4().hex
    container, network, other = f"yp-m51-expired-{suffix}", f"yp-m51-net-{suffix}", f"yp-m51-other-{suffix}"
    labels = ["--label", "y-point.managed=tool", "--label", f"y-point.scope={scope}", "--label", "y-point.deadline=1"]
    subprocess.run(["docker", "network", "create", *labels, network], check=True, capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", container, *labels, "--network=none", config.SCRIPT_SANDBOX_IMAGE, "sleep", "300"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "docker",
            "network",
            "create",
            "--label",
            "y-point.managed=tool",
            "--label",
            "y-point.scope=other",
            "--label",
            "y-point.deadline=1",
            other,
        ],
        check=True,
        capture_output=True,
    )
    for name in ("build-crashed", "b" * 64, "a" * 64):
        path = root / name
        path.mkdir()
        mark_resource_directory(path, -int(time.time()))

    async def exercise():
        """保存历史版本引用，核对清理范围与幂等性。"""
        async with _database(tmp_path) as sessions:
            async with sessions.begin() as db:
                db.add(
                    UserToolVersion(
                        tool_id=999,
                        version=1,
                        content_hash="a" * 64,
                        input_schema={
                            "type": "object",
                        },
                        effect=ToolEffect.READ,
                        requires_approval=False,
                        verified=True,
                        environment_key="a" * 64,
                        is_deleted=1,
                        status=1,
                    ),
                )
            result = await prune_tool_resources(sessions)
            assert result["containers"] == 1 and result["networks"] == 1 and result["directories"] == 2
            assert (root / ("a" * 64)).exists() and not (root / "build-crashed").exists() and not (root / ("b" * 64)).exists()
            assert subprocess.run(["docker", "network", "inspect", other], capture_output=True).returncode == 0
            repeated = await prune_tool_resources(sessions)
            assert repeated["containers"] == repeated["networks"] == repeated["directories"] == 0

    try:
        asyncio.run(exercise())
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        subprocess.run(["docker", "network", "rm", network, other], capture_output=True)


class GovernanceModel(FakeMessagesListChatModel):
    """通过真实运行图验证审批与外部写结果不明时的处理。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """使用固定调用序列，隔离上游模型随机性。"""
        return self


@pytest.mark.parametrize("remote_fails", [False, True])
def test_external_write_injects_idempotency_and_never_replays(tmp_path, monkeypatch, remote_fails) -> None:
    """远端写调用收到服务端幂等键，失败保留 unknown，同一模型调用不会重复发送。"""
    calls = []

    async def remote(url, name, arguments, token, timeout_seconds, expected_schema, **options):
        """模拟已产生外部副作用后丢失返回值。"""
        calls.append(arguments["request_id"])
        assert options["expected_read_only"] is False
        if remote_fails:
            raise TimeoutError("remote result lost")
        return '{"created": true}'

    monkeypatch.setattr("app.services.worker_tools.invoke_mcp_tool", remote)

    async def exercise():
        """依次提交、审批、执行和恢复，核对实际调用次数及审计状态。"""
        async with _database(tmp_path) as sessions:
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="write-owner", password="unused", role=UserRole.USER, status=1, is_deleted=0))
                db.add(
                    UserTool(
                        id=3,
                        owner_id=1,
                        name="create_record",
                        description="幂等创建记录",
                        kind=ToolKind.MCP,
                        visibility=Visibility.PRIVATE,
                        enabled=True,
                        current_version=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    UserToolVersion(
                        tool_id=3,
                        version=1,
                        content_hash="a" * 64,
                        input_schema={
                            "type": "object",
                            "properties": {
                                "request_id": {
                                    "type": "string",
                                },
                            },
                            "required": ["request_id"],
                            "additionalProperties": False,
                        },
                        effect=ToolEffect.WRITE,
                        recovery_policy=RecoveryPolicy.IDEMPOTENT,
                        idempotency_key_field="request_id",
                        write_contract_verified=True,
                        requires_approval=True,
                        verified=True,
                        mcp_url="https://mcp.example.com/mcp",
                        mcp_tool_name="create_record",
                        share_credentials=False,
                        status=1,
                        is_deleted=0,
                    ),
                )
                await db.flush()
                run = await create_run(db, 1, input_text="创建一次记录")
                db.add(RunTool(run_id=run.id, tool_id=3, tool_version=1, content_hash="a" * 64))
            call = {
                "name": "user_tool_3_create_record",
                "args": {},
                "id": "stable-model-call",
            }
            model = GovernanceModel(responses=[AIMessage(content="", tool_calls=[call]), AIMessage(content="", tool_calls=[call]), AIMessage(content="完成")])
            checkpoint = tmp_path / "write-checkpoints.db"
            result = await execute_text_run(sessions, run.id, model, checkpoint, resolve_tools=make_worker_tool_resolver(sessions))
            for _ in range(3):
                if result.state != "waiting_for_approval":
                    break
                async with sessions.begin() as db:
                    await decide_approval(db, run.id, 1, "approve")
                result = await execute_text_run(sessions, run.id, model, checkpoint, resolve_tools=make_worker_tool_resolver(sessions), resume=True)
            assert result.state == ("timed_out" if remote_fails else "failed"), result.failure_code
            assert len(calls) == 1, result.failure_code
            async with sessions() as db:
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
                assert invocation.call_id == calls[0]
                assert invocation.state == ("unknown" if remote_fails else "completed")
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0

    asyncio.run(exercise())


def test_credential_revoke_closes_approval_and_signals_running_run(tmp_path, monkeypatch) -> None:
    """撤销凭据同时关闭待审批 Run，执行中的 Run 收到跨进程可见的取消请求。"""
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "1.1.1.1")

    async def discover(url, token):
        """提供真实 API 所需的只读工具发现信息。"""
        return [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                },
                "read_only": True,
            },
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def exercise():
        """分别构造等待审批及运行中任务，再经 HTTP 撤销凭据。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            response = await client.post(
                "/api/v1/tools",
                headers=owner,
                json={
                    "name": "revoke_lookup",
                    "description": "查询",
                    "kind": "mcp",
                    "mcp_url": "https://mcp.example.com/mcp",
                    "mcp_tool_name": "lookup",
                    "mcp_bearer_token": "do-not-leak",
                },
            )
            assert response.status_code == 201, response.text
            tool = response.json()["data"]
            runs = []
            for waiting in (False, True):
                async with sessions.begin() as db:
                    run = await create_run(db, 1, input_text="test revoke")
                    db.add(RunTool(run_id=run.id, tool_id=tool["id"], tool_version=1, content_hash=tool["content_hash"]))
                    claimed = await claim_run(db, run.id)
                    if waiting:
                        await create_approval_request(
                            db,
                            run.id,
                            claimed.lease_token,
                            "checkpoint",
                            [
                                {
                                    "name": tool["model_name"],
                                    "arguments_hash": "a" * 64,
                                },
                            ],
                        )
                    runs.append(run.id)
            revoked = await client.post(
                f"/api/v1/tools/{tool['id']}/credentials/revoke",
                headers=owner,
                json={
                    "expected_version": 1,
                },
            )
            assert revoked.status_code == 200 and revoked.json()["data"]["cancelled_runs"] == 2
            async with sessions() as db:
                assert (await db.get(AgentRun, runs[0])).state == "cancel_requested"
                assert (await db.get(AgentRun, runs[1])).state == "cancelled"

    asyncio.run(exercise())


def test_write_contract_requires_admin_evidence_and_resets_for_new_version(tmp_path, monkeypatch) -> None:
    """普通用户不能自行开放外部写，管理员验收只适用于指定不可变版本。"""
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "1.1.1.1")

    async def discover(url, token):
        """发现具有明确字符串幂等键的外部写工具。"""
        return [
            {
                "name": "create_record",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {
                            "type": "string",
                        },
                    },
                },
                "read_only": False,
            },
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def exercise():
        """通过真实权限入口验收恢复契约并核对新版本重新验证。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, administrator = headers
            response = await client.post(
                "/api/v1/tools",
                headers=owner,
                json={
                    "name": "verified_write",
                    "description": "幂等写入",
                    "kind": "mcp",
                    "mcp_url": "https://mcp.example.com/mcp",
                    "mcp_tool_name": "create_record",
                    "effect": "write",
                    "recovery_policy": "idempotent",
                    "idempotency_key_field": "request_id",
                },
            )
            assert response.status_code == 201, response.text
            tool_id = response.json()["data"]["id"]
            assert response.json()["data"]["write_contract_verified"] is False
            path = f"/api/v1/tools/{tool_id}/write-contract/verify"
            evidence = {
                "expected_version": 1,
                "evidence_reference": "docs/example-write-evidence.md#duplicate-and-timeout",
            }
            assert (await client.post(path, headers=owner, json=evidence)).status_code == 403
            async with sessions.begin() as db:
                admin = await db.get(Admin, 2)
                admin.role = UserRole.ADMIN
            checked = await client.post(path, headers=administrator, json=evidence)
            assert checked.status_code == 200, checked.text
            async with sessions() as db:
                version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == 1))
                assert version.write_contract_verified and len(version.write_contract_evidence_hash) == 64
            updated = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 1,
                    "description": "新版写入说明",
                },
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["data"]["write_contract_verified"] is False
            assert (await client.post(path, headers=administrator, json=evidence)).status_code == 409

    asyncio.run(exercise())


def test_mcp_discovery_api_rejects_capacity_without_contacting_upstream(tmp_path, monkeypatch) -> None:
    """发现与正式调用共享名额，容量满时返回 429 且不请求远端。"""
    monkeypatch.setattr(get_config(), "TOOL_CALL_USER_LIMIT", 1)
    contacted = []

    async def discover(url, token):
        """记录意外上游请求。"""
        contacted.append(True)
        return []

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def exercise():
        """占满实际用户名额后通过 HTTP 发起发现请求。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            async with tool_execution_slot(sessions, 1, None, ToolLeaseKind.CALL, 10):
                response = await client.post(
                    "/api/v1/tools/mcp/discover",
                    headers=headers[0],
                    json={
                        "url": "https://mcp.example.com/mcp",
                    },
                )
                assert response.status_code == 429, response.text
                assert contacted == []

    asyncio.run(exercise())


@pytest.mark.parametrize("preparation", [False, True])
def test_sigkill_host_reclaims_real_container_proxy_network_and_directory(tmp_path, monkeypatch, preparation) -> None:
    """强杀实际执行宿主后，按持久化标签回收容器、代理、网络及临时目录。"""
    config = get_config()
    root = tmp_path / "kill-environments"
    monkeypatch.setattr(config, "SCRIPT_DEPENDENCY_STORAGE_DIR", str(root))
    monkeypatch.setattr(config, "TOOL_STORAGE_MIN_FREE_BYTES", 0)
    monkeypatch.setattr("app.services.tool_resources.BASE_DIR", tmp_path)
    script = """
import asyncio, os
from pathlib import Path
from app.services import script_sandbox
from app.services.tool_environments import _package_network
from app.services.tool_resources import managed_workspace, dependency_root
from app.core.config import get_config
async def exercise():
    '''创建真实资源后等待父进程强杀宿主。'''
    if os.environ["YPOINT_KILL_PREPARATION"] == "1":
        with managed_workspace(dependency_root(), "build-", 60):
            async with _package_network(get_config().SCRIPT_SANDBOX_IMAGE):
                print("READY", flush=True)
                await asyncio.Event().wait()
    else:
        script_sandbox.SANDBOX_ROOT = Path(os.environ["YPOINT_KILL_ROOT"]) / ".run-private" / "tool-sandbox"
        await script_sandbox.run_script("python", "import time; time.sleep(60)", {}, 60)
asyncio.run(exercise())
"""
    env = {
        **os.environ,
        "SCRIPT_DEPENDENCY_STORAGE_DIR": str(root),
        "YPOINT_KILL_ROOT": str(tmp_path),
        "YPOINT_KILL_PREPARATION": "1" if preparation else "0",
    }
    process = subprocess.Popen([sys.executable, "-c", script], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    scope = resource_scope()
    filters = ["--filter", "label=y-point.managed=tool", "--filter", f"label=y-point.scope={scope}"]

    def docker(*arguments):
        """只操作本测试唯一作用域内的资源。"""
        return subprocess.check_output(["docker", *arguments, *filters], text=True).split()

    async def exercise():
        """等待资源真实启动，强杀宿主，再加速宽限期验证清理。"""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            containers = docker("ps", "-q")
            if containers:
                break
            assert process.poll() is None, "资源宿主提前退出"
            await asyncio.sleep(0.1)
        else:
            pytest.fail("未观察到本测试容器启动")
        if preparation:
            output = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), timeout=10)
            assert output.strip() == b"READY"
        process.kill()
        await asyncio.to_thread(process.wait, timeout=10)
        assert docker("ps", "-q"), "强杀后应存在待核对的容器"
        now = time.time()
        monkeypatch.setattr("app.services.tool_resources.time.time", lambda: now + 1000)
        async with _database(tmp_path) as sessions:
            result = await prune_tool_resources(sessions)
            assert result["containers"] == 1
            assert result["networks"] == (1 if preparation else 0)
            assert result["directories"] == 1
            assert docker("ps", "-aq") == [] and docker("network", "ls", "-q") == []

    try:
        asyncio.run(exercise())
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        for identifier in docker("ps", "-aq"):
            subprocess.run(["docker", "rm", "-f", identifier], capture_output=True)
        for identifier in docker("network", "ls", "-q"):
            subprocess.run(["docker", "network", "rm", identifier], capture_output=True)


def test_mcp_sdk_authenticated_discovery_rotation_and_failures(tmp_path: Path, monkeypatch) -> None:
    """真实 MCP SDK 经 HTTP 协议验证有效、失效、轮换凭据及限流，并保持 API 错误脱敏。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations
    from starlette.responses import PlainTextResponse

    from app.services import mcp_tools

    server = FastMCP(
        "governance-auth",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def double(value: int) -> int:
        """读取整数并返回两倍，供正式 SDK 完成发现及调用。"""
        return value * 2

    application = server.streamable_http_app()
    accepted_token = "m51-old-test-token"
    limited = False
    received_tokens = []

    async def authenticated(scope, receive, send):
        """在 SDK 服务前模拟真实 Bearer 鉴权及远端 429，拒绝正文包含敏感内容。"""
        token = dict(scope["headers"]).get(b"authorization", b"").decode()
        received_tokens.append(token)
        if token != f"Bearer {accepted_token}":
            response = PlainTextResponse(f"invalid credential: {token}", status_code=401)
            await response(scope, receive, send)
        elif limited:
            response = PlainTextResponse(f"limited credential: {token}", status_code=429)
            await response(scope, receive, send)
        else:
            await application(scope, receive, send)

    def initialize_transport(self, host, address):
        """只将最底层网络换为进程内 HTTP，上层限额及 SDK 协议保持正式实现。"""
        self.host = host
        self.address = address
        self.received = 0
        self.max_bytes = get_config().MCP_RESPONSE_MAX_BYTES
        self.transport = httpx.ASGITransport(app=authenticated)

    monkeypatch.setattr(mcp_tools, "validate_mcp_url", lambda url: "1.1.1.1")
    monkeypatch.setattr(_PinnedPublicTransport, "__init__", initialize_transport)

    async def exercise():
        """轮换远端接受的凭据，核对正式发现路由及调用结果。"""
        nonlocal accepted_token, limited
        async with server.session_manager.run(), _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, _ = headers

            async def discover(token):
                """通过正式业务接口执行鉴权发现。"""
                return await client.post(
                    "/api/v1/tools/mcp/discover",
                    headers=owner,
                    json={
                        "url": "https://mcp.example.com/mcp",
                        "bearer_token": token,
                    },
                )

            response = await discover(accepted_token)
            assert response.status_code == 200, response.text
            remote = response.json()["data"][0]
            assert remote["name"] == "double" and remote["read_only"] is True
            result = await mcp_tools.invoke_mcp_tool(
                "https://mcp.example.com/mcp",
                "double",
                {
                    "value": 21,
                },
                accepted_token,
                5,
                remote["input_schema"],
                expected_read_only=True,
            )
            assert "42" in result
            accepted_token = "m51-new-test-token"
            failed = await discover("m51-old-test-token")
            assert failed.status_code == 409 and "test-token" not in failed.text
            assert (await discover(accepted_token)).status_code == 200
            limited = True
            failed = await discover(accepted_token)
            assert failed.status_code == 409 and "test-token" not in failed.text
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0
        assert "Bearer m51-old-test-token" in received_tokens and "Bearer m51-new-test-token" in received_tokens

    asyncio.run(exercise())
