# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_run_sessions.py

"""以实际协议和隔离业务库验证 Run MCP 会话跨审批、归属及资源清理。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import socket
import sys

import httpx
import pytest
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import uvicorn

from app.core.config import get_config
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import Base, shanghai_now
from app.services import mcp_tools, mcp_stdio
from app.services.mcp_sessions import RunMcpSessions, McpSessionLostError, expire_mcp_owners
from app.services.run_manager import claim_run, create_run, create_approval_request, decide_approval, resume_approved_run, request_cancel
from app.services.tool_delivery import ToolNotSentError


@asynccontextmanager
async def database(path):
    """测试只创建临时数据库，不访问实际项目数据。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as db:
        db.add_all([Admin(id=index, username=f"sessions-{index}", password="unused", status=1, is_deleted=0) for index in (1, 2)])
    try:
        yield sessions
    finally:
        await engine.dispose()


async def running(sessions, owner=1):
    """领取一个具有正常业务租约的独立 Run。"""
    async with sessions.begin() as db:
        run = await create_run(db, owner, input_text="检查浏览器状态")
        return await claim_run(db, run.id, lease_seconds=60)


@asynccontextmanager
async def http_server(monkeypatch):
    """真实 TCP + 官方有状态 MCP Server；记录握手、调用、取消与 DELETE。"""
    remote = FastMCP("stateful", json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    counters, requests, deleted = {}, [], []
    entered = asyncio.Event()
    entered.release = asyncio.Event()

    @remote.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def inspect(value: str, ctx: Context) -> str:
        """用协议会话身份保留页面状态；慢调用模拟可能已经执行的远端操作。"""
        identity = id(ctx.session)
        counters[identity] = counters.get(identity, 0) + 1
        if value == "yield":
            await asyncio.sleep(0.05)
        if value == "slow":
            entered.set()
            await entered.release.wait()
        return f"{identity}:{counters[identity]}:{value}"

    application = remote.streamable_http_app()

    async def observe(scope, receive, send):
        """观察真实收包，不替换 SDK 或协议消息。"""
        if scope["type"] != "http":
            return await application(scope, receive, send)
        body = bytearray()

        async def traced_receive():
            message = await receive()
            body.extend(message.get("body", b""))
            if message["type"] == "http.request" and not message.get("more_body", False) and scope["method"] == "POST" and body:
                requests.append(json.loads(body))
            return message

        async def traced_send(message):
            await send(message)

        if scope["method"] == "DELETE":
            deleted.append(dict(scope["headers"]).get(b"mcp-session-id"))
        await application(scope, traced_receive, traced_send)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(observe, log_level="critical", lifespan="on", timeout_graceful_shutdown=0.5))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(5):
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
    monkeypatch.setattr(mcp_tools, "validate_mcp_url", lambda url: "127.0.0.1")
    try:
        yield f"http://127.0.0.1:{port}/mcp", requests, deleted, entered
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


async def call(pool, run, url, schema, value="page", timeout=5, connection=11):
    """走正式 MCP 调用入口，使用真实 Schema 校验和会话池。"""
    return await mcp_tools.invoke_mcp_tool(
        url,
        "inspect",
        {
            "value": value,
        },
        None,
        timeout,
        schema,
        expected_read_only=True,
        session_pool=pool,
        session_identity={
            "run_id": run.id,
            "owner_id": run.owner_id,
            "lease_token": run.lease_token,
            "connection_id": connection,
        },
    )


def text(result):
    """提取远端受控结果以判断状态是否延续。"""
    return json.loads(result)["content"][0]["text"]


def test_http_sessions_survive_approval_and_isolate_users_runs(tmp_path, monkeypatch):
    """同 Run 跨租约保留状态；同用户其他 Run、不同用户均不能继承。"""

    async def exercise():
        async with database(tmp_path / "sessions.db") as sessions, http_server(monkeypatch) as (url, requests, deleted, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            deleted.clear()
            pool = RunMcpSessions(sessions, "worker-a")
            other = RunMcpSessions(sessions, "worker-b")
            try:
                run = await running(sessions)
                first = text(await call(pool, run, url, schema))
                second = text(await call(pool, run, url, schema, "opened"))
                assert first.split(":")[0] == second.split(":")[0] and ":2:opened" in second
                async with sessions.begin() as db:
                    approval = await create_approval_request(
                        db,
                        run.id,
                        run.lease_token,
                        "checkpoint",
                        [
                            {
                                "name": "inspect",
                                "arguments_hash": "a" * 64,
                            }
                        ],
                    )
                    await decide_approval(db, run.id, 1, "reject_continue", expected_approval_id=approval.id)
                await pool.sweep()
                async with sessions.begin() as db:
                    run = await resume_approved_run(db, run.id)
                third = text(await call(pool, run, url, schema, "read"))
                assert ":3:read" in third and third.split(":")[0] == first.split(":")[0]
                with pytest.raises(ToolNotSentError):
                    await call(other, run, url, schema)
                run2, run3 = await running(sessions), await running(sessions, 2)
                assert ":1:page" in text(await call(pool, run2, url, schema))
                assert ":1:page" in text(await call(pool, run3, url, schema))
                assert len(pool.entries) == 3
                async with sessions.begin() as db:
                    await request_cancel(db, run.id, 1)
                await pool.sweep()
                assert len(pool.entries) == 2 and len(deleted) == 1
                # 一次独立发现 + 三个隔离 Run，审批不增加握手。
                assert sum(item.get("method") == "initialize" for item in requests) == 4
            finally:
                await pool.aclose()
                await other.aclose()
            assert len(deleted) == 3

    asyncio.run(exercise())


@pytest.mark.parametrize("expiry", ["idle", "lifetime", "crash", "credentials", "quota"])
def test_http_session_loss_and_quota_never_replay(tmp_path, monkeypatch, expiry):
    """过期与失主不自动新建；资源上限在业务发送之前拒绝。"""

    async def exercise():
        async with database(tmp_path / "loss.db") as sessions, http_server(monkeypatch) as (url, requests, deleted, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            pool = RunMcpSessions(sessions, "worker-a")
            run = await running(sessions)
            try:
                await call(pool, run, url, schema)
                if expiry == "quota":
                    monkeypatch.setattr(get_config(), "MCP_SESSION_MAX_PER_RUN", 1)
                    with pytest.raises(ToolNotSentError):
                        await call(pool, run, url, schema, connection=22)
                    assert len(pool.entries) == 1
                    return
                if expiry in {"idle", "lifetime"}:
                    entry = next(iter(pool.entries.values()))
                    if expiry == "idle":
                        entry.touched -= get_config().MCP_SESSION_IDLE_SECONDS + 1
                    else:
                        entry.created -= get_config().MCP_SESSION_MAX_SECONDS + 1
                    await pool.sweep()
                elif expiry == "crash":
                    async with sessions.begin() as db:
                        row = await db.get(AgentRun, run.id)
                        row.mcp_owner_expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
                    async with sessions.begin() as db:
                        assert await expire_mcp_owners(db) == 1
                    await pool.sweep()
                else:
                    with pytest.raises(McpSessionLostError):
                        await pool.invoke(
                            run_id=run.id,
                            owner_id=1,
                            lease_token=run.lease_token,
                            connection_id=11,
                            config=url,
                            credentials="changed",
                            factory=None,
                            operation=None,
                        )
                    return
                with pytest.raises(Exception):
                    await call(pool, run, url, schema)
                async with sessions() as db:
                    row = await db.get(AgentRun, run.id)
                    assert row.state == "failed" and row.failure_code == "mcp_session_lost"
                assert sum(item.get("method") == "tools/call" for item in requests) == 1
                assert deleted
            finally:
                await pool.aclose()

    asyncio.run(exercise())


def test_http_timeout_sends_cancel_then_deletes_without_replay(tmp_path, monkeypatch):
    """SDK 1.30.0 的实际远端收包包括匹配 ID 的取消，且只执行一次业务调用。"""

    async def exercise():
        async with database(tmp_path / "timeout.db") as sessions, http_server(monkeypatch) as (url, requests, deleted, entered):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            deleted.clear()
            pool = RunMcpSessions(sessions, "worker-a")
            run = await running(sessions)
            try:
                with pytest.raises(TimeoutError):
                    await call(pool, run, url, schema, "slow", timeout=0.3)
                assert entered.is_set()
                calls = [item for item in requests if item.get("method") == "tools/call"]
                cancelled = [item for item in requests if item.get("method") == "notifications/cancelled"]
                assert len(calls) == 1 and cancelled[0]["params"]["requestId"] == calls[0]["id"]
                assert len(deleted) == 1 and not pool.entries
            finally:
                await pool.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("stop", ["cancel", "timeout", "expired", "revoked"])
def test_stdio_run_reuse_and_protocol_cleanup(tmp_path, monkeypatch, stop):
    """真实子进程跨审批延续状态；取消/超时真实收包且进程、管道、临时凭据均回收。"""
    from app.services.mcp_connections import connection_config

    profile = {
        "image": "sha256:" + "a" * 64,
        "command": ["python", "/app/server.py"],
        "env_keys": [],
    }
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "session": profile,
        },
    )
    monkeypatch.setattr(mcp_stdio, "SANDBOX_ROOT", tmp_path / "sandbox")
    processes, removed, commands = [], [], []
    log = tmp_path / "protocol.jsonl"

    async def spawn(command, limit):
        """替换容器客户端，保留真实子进程管道和正式所有回收逻辑。"""
        commands.append(command)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(Path(__file__).parent / "fixtures" / "mcp_session_server.py"),
            str(log),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=limit + 1,
        )
        processes.append(process)
        return process

    async def remove(name):
        """记录容器删除调用，不访问实际 Docker。"""
        removed.append(name)

    monkeypatch.setattr(mcp_stdio, "_spawn_stdio", spawn)
    monkeypatch.setattr(mcp_stdio, "_remove_container", remove)
    url = connection_config(
        {
            "transport": "stdio",
            "stdio_profile": "session",
            "args": [],
        }
    )
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
            },
        },
        "required": ["value"],
    }

    async def exercise():
        async with database(tmp_path / "stdio.db") as sessions:
            pool = RunMcpSessions(sessions, "stdio-worker")
            run = await running(sessions)
            try:
                assert text(await call(pool, run, url, schema, "open")) == "stdio:1:open"
                async with sessions.begin() as db:
                    approval = await create_approval_request(
                        db,
                        run.id,
                        run.lease_token,
                        "paused",
                        [
                            {
                                "name": "inspect",
                                "arguments_hash": "a" * 64,
                            }
                        ],
                    )
                    await decide_approval(db, run.id, 1, "approve", expected_approval_id=approval.id)
                async with sessions.begin() as db:
                    run = await resume_approved_run(db, run.id)
                assert text(await call(pool, run, url, schema, "read")) == "stdio:2:read"
                assert len(processes) == 1
                if stop == "timeout":
                    with pytest.raises(TimeoutError):
                        await call(pool, run, url, schema, "slow", timeout=0.2)
                elif stop == "cancel":
                    pending = asyncio.create_task(call(pool, run, url, schema, "slow"))
                    async with asyncio.timeout(3):
                        while '"slow"' not in log.read_text():
                            await asyncio.sleep(0.01)
                    pending.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await pending
                elif stop == "revoked":
                    async with sessions.begin() as db:
                        (await db.get(Admin, 1)).status = 0
                    await pool.sweep()
                else:
                    next(iter(pool.entries.values())).created -= get_config().MCP_SESSION_MAX_SECONDS + 1
                    await pool.sweep()
                assert not pool.entries and processes[0].returncode is not None and len(removed) == 1
                assert not list((tmp_path / "sandbox").glob("mcp-*"))
                requests = [json.loads(line) for line in log.read_text().splitlines()]
                assert sum(item["method"] == "initialize" for item in requests) == 1
                if stop in {"timeout", "cancel"}:
                    calls = [item for item in requests if item["method"] == "tools/call"]
                    cancellations = [item for item in requests if item["method"] == "notifications/cancelled"]
                    assert len(calls) == 3 and cancellations[0]["params"]["requestId"] == calls[-1]["id"]
                assert "--network=none" in commands[0] and "--pull=never" in commands[0]
                deadline = next(item for item in commands[0] if item.startswith("y-point.deadline="))
                assert int(deadline.split("=")[1]) > __import__("time").time() + get_config().MCP_CALL_TIMEOUT_SECONDS
            finally:
                await pool.aclose()

    asyncio.run(exercise())


def test_http_404_does_not_reinitialize_or_replay(tmp_path, monkeypatch):
    """远端会话消失后保留一次原操作事实，不握手新会话补做同一业务请求。"""

    async def exercise():
        async with database(tmp_path / "404.db") as sessions, http_server(monkeypatch) as (url, requests, _, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            pool = RunMcpSessions(sessions, "worker-404")
            run = await running(sessions)
            try:
                await call(pool, run, url, schema)
                original = mcp_tools._PinnedMcpTransport.handle_async_request

                async def missing(transport, request):
                    """只让既有会话的后续请求获得真实 HTTP 404 响应。"""
                    if request.method == "POST":
                        return httpx.Response(
                            404,
                            json={
                                "error": "session lost",
                            },
                            request=request,
                        )
                    return await original(transport, request)

                monkeypatch.setattr(mcp_tools._PinnedMcpTransport, "handle_async_request", missing)
                with pytest.raises(ToolNotSentError):
                    await call(pool, run, url, schema)
                await pool.sweep()
                async with sessions() as db:
                    row = await db.get(AgentRun, run.id)
                    assert row.failure_code == "mcp_session_lost" and row.state == "failed"
                assert sum(item.get("method") == "tools/call" for item in requests) == 1
                assert sum(item.get("method") == "initialize" for item in requests) == 2  # 独立发现和首个 Run 会话
            finally:
                await pool.aclose()

    asyncio.run(exercise())


def test_real_worker_routing_preserves_http_state_across_approval(tmp_path, monkeypatch):
    """正式工具解析、图检查点与调度共同验证：另一 Worker 不抢，原 Worker 复用。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from app.models.tool import RunTool, UserTool, UserToolVersion
    from app.services.run_dispatcher import dispatch_next_run
    from app.services.worker_tools import make_worker_tool_resolver

    class Model(FakeMessagesListChatModel):
        """仅替换模型上游，MCP 与调度均使用正式实现。"""

        def bind_tools(self, tools, **kwargs):
            """绑定正式 Worker 工具。"""
            return self

    async def exercise():
        async with database(tmp_path / "worker.db") as sessions, http_server(monkeypatch) as (url, requests, deleted, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            async with sessions.begin() as db:
                # 两个固定 Tool 共享同一远端连接，但审批策略不同。
                from app.models.mcp_connection import McpConnection

                db.add(
                    McpConnection(
                        id=11,
                        owner_id=1,
                        name="browser",
                        config={
                            "transport": "streamable_http",
                            "url": url,
                        },
                        version=1,
                        enabled=True,
                    )
                )
                for index in (7, 8):
                    db.add(
                        UserTool(
                            id=index,
                            owner_id=1,
                            name=f"inspect_{index}",
                            description="检查页面",
                            kind="mcp",
                            visibility="private",
                            enabled=True,
                            current_version=1,
                        )
                    )
                    db.add(
                        UserToolVersion(
                            tool_id=index,
                            version=1,
                            content_hash=str(index) * 64,
                            input_schema=schema,
                            effect="read",
                            recovery_policy="safe",
                            requires_approval=index == 8,
                            verified=True,
                            mcp_connection_id=11,
                            mcp_connection_version=1,
                            mcp_url=url,
                            mcp_tool_name="inspect",
                        )
                    )
                run = await create_run(db, 1, input_text="检查并打开页面")
                for index in (7, 8):
                    db.add(RunTool(run_id=run.id, tool_id=index, tool_version=1, content_hash=str(index) * 64, requires_approval=index == 8))
            responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "user_tool_7_inspect_7",
                            "args": {
                                "value": "inspect",
                            },
                            "id": "one",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "user_tool_8_inspect_8",
                            "args": {
                                "value": "open",
                            },
                            "id": "two",
                        }
                    ],
                ),
                AIMessage(content="已检查并打开页面"),
            ]
            model = Model(responses=responses)

            async def resolve_model(db, current):
                """跨调度重建运行时，保留受控模型响应序列。"""
                return model

            pool = RunMcpSessions(sessions, "original-worker")
            other = RunMcpSessions(sessions, "new-worker")
            checkpoint = tmp_path / "checkpoint.db"
            try:
                paused = await dispatch_next_run(
                    sessions,
                    checkpoint,
                    resolve_model=resolve_model,
                    resolve_tools=make_worker_tool_resolver(sessions, mcp_sessions=pool),
                    mcp_worker_id=pool.worker_id,
                )
                assert paused.state == "waiting_for_approval", paused.failure_code
                assert len(pool.entries) == 1 and len(requests) > 0
                async with sessions.begin() as db:
                    from app.services.run_manager import get_owned_approval

                    saved_approval = await get_owned_approval(db, run.id, 1)
                    assert saved_approval.expires_at is None
                # 用已经过去一年的本地计时模拟长时间人工等待，连接本身保持真实。
                from sqlalchemy import select
                from app.models.run_mcp_resource import RunMcpResource
                from app.services.mcp_sessions import cleanup_abandoned_sessions

                entry = next(iter(pool.entries.values()))
                assert entry.approval_paused_at is not None
                deletions_before_wait = len(deleted)
                waited = 365 * 86400
                entry.created -= waited
                entry.touched -= waited
                entry.approval_paused_at -= waited
                async with sessions.begin() as db:
                    receipt = await db.scalar(select(RunMcpResource))
                    receipt.expires_at = shanghai_now(naive=True) - timedelta(days=365)
                await pool.sweep()
                assert len(pool.entries) == 1 and len(deleted) == deletions_before_wait
                assert await cleanup_abandoned_sessions(sessions) == 0
                async with sessions.begin() as db:
                    receipt = await db.scalar(select(RunMcpResource))
                    assert receipt.expires_at > shanghai_now(naive=True)
                    assert (await db.get(AgentRun, run.id)).state == "waiting_for_approval"
                    await decide_approval(db, run.id, 1, "approve")
                assert (
                    await dispatch_next_run(
                        sessions,
                        checkpoint,
                        resolve_model=resolve_model,
                        resolve_tools=make_worker_tool_resolver(sessions, mcp_sessions=other),
                        mcp_worker_id=other.worker_id,
                    )
                    is None
                )
                completed = await dispatch_next_run(
                    sessions,
                    checkpoint,
                    resolve_model=resolve_model,
                    resolve_tools=make_worker_tool_resolver(sessions, mcp_sessions=pool),
                    mcp_worker_id=pool.worker_id,
                )
                assert completed.state == "completed", completed.failure_code
                await pool.sweep()
                assert not pool.entries
                assert sum(item.get("method") == "initialize" for item in requests) == 2
                assert sum(item.get("method") == "tools/call" for item in requests) == 2
            finally:
                await pool.aclose()
                await other.aclose()

    asyncio.run(exercise())


def test_real_write_timeout_keeps_unknown_result_and_never_replays(tmp_path, monkeypatch):
    """正式审批后的外部写已送达才超时，UNKNOWN 审计不因清理会话变成可自动重试。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from sqlalchemy import select
    from app.models.tool import RunTool, UserTool, UserToolVersion, ToolInvocation
    from app.services.run_dispatcher import dispatch_next_run
    from app.services.worker_tools import make_worker_tool_resolver

    class Model(FakeMessagesListChatModel):
        """调用慢写工具，然后应由平台终止。"""

        def bind_tools(self, tools, **kwargs):
            """接受正式工具绑定。"""
            return self

    async def exercise():
        async with database(tmp_path / "write.db") as sessions, http_server(monkeypatch) as (url, requests, deleted, entered):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            monkeypatch.setattr(get_config(), "MCP_CALL_TIMEOUT_SECONDS", 0.3)
            async with sessions.begin() as db:
                db.add(
                    UserTool(
                        id=9, owner_id=1, name="external_write", description="外部写操作", kind="mcp", visibility="private", enabled=True, current_version=1
                    )
                )
                db.add(
                    UserToolVersion(
                        tool_id=9,
                        version=1,
                        content_hash="9" * 64,
                        input_schema=schema,
                        effect="write",
                        recovery_policy="manual",
                        requires_approval=True,
                        verified=True,
                        mcp_url=url,
                        mcp_tool_name="inspect",
                    )
                )
                run = await create_run(db, 1, input_text="执行外部写操作")
                db.add(RunTool(run_id=run.id, tool_id=9, tool_version=1, content_hash="9" * 64, requires_approval=True))
            model = Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "user_tool_9_external_write",
                                "args": {
                                    "value": "slow",
                                },
                                "id": "write-one",
                            }
                        ],
                    )
                ]
            )

            async def resolve_model(db, current):
                """只生成一次写调用。"""
                return model

            pool = RunMcpSessions(sessions, "write-worker")
            resolver = make_worker_tool_resolver(sessions, mcp_sessions=pool)
            checkpoint = tmp_path / "write-checkpoint.db"
            try:
                paused = await dispatch_next_run(sessions, checkpoint, resolve_model=resolve_model, resolve_tools=resolver, mcp_worker_id=pool.worker_id)
                assert paused.state == "waiting_for_approval"
                async with sessions.begin() as db:
                    await decide_approval(db, run.id, 1, "approve")
                result = await dispatch_next_run(sessions, checkpoint, resolve_model=resolve_model, resolve_tools=resolver, mcp_worker_id=pool.worker_id)
                assert result.state in {"failed", "timed_out"} and entered.is_set()
                async with sessions() as db:
                    invocations = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id))).all()
                    assert len(invocations) == 1 and invocations[0].state == "unknown" and invocations[0].failure_code == "external_result_unknown"
                assert sum(item.get("method") == "tools/call" for item in requests) == 1
                assert any(item.get("method") == "notifications/cancelled" for item in requests)
                assert not pool.entries
            finally:
                await pool.aclose()

    asyncio.run(exercise())


def test_concurrent_calls_share_one_session_and_serialize_state(tmp_path, monkeypatch):
    """同连接按授权校验完成后的入队顺序串行，不承诺并行任务的创建顺序。"""

    async def exercise():
        async with database(tmp_path / "ordered.db") as sessions, http_server(monkeypatch) as (url, requests, _, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            pool = RunMcpSessions(sessions, "ordered-worker")
            run = await running(sessions)
            try:
                # 预先建立会话，随后并发提交四项，SDK 同连接只运行一项。
                await call(pool, run, url, schema)
                results = await asyncio.gather(*(call(pool, run, url, schema, "yield") for _ in range(4)))
                assert sorted(int(text(result).split(":")[1]) for result in results) == [2, 3, 4, 5]
                assert len(pool.entries) == 1
                assert sum(item.get("method") == "initialize" for item in requests) == 2
            finally:
                await pool.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["crash", "retired", "retry"])
def test_other_worker_cleans_encrypted_http_resource_without_business_replay(tmp_path, monkeypatch, mode):
    """失主会话由其他 Worker 用加密凭单 DELETE；失败节流后重试，不重新握手或执行业务。"""
    from sqlalchemy import select
    from app.models.run_mcp_resource import RunMcpResource
    from app.services.mcp_sessions import cleanup_abandoned_sessions

    async def exercise():
        async with database(tmp_path / "orphan.db") as sessions, http_server(monkeypatch) as (url, requests, deleted, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            deleted.clear()
            pool = RunMcpSessions(sessions, "crashed-worker")
            run = await running(sessions)
            try:
                await call(pool, run, url, schema)
                async with sessions.begin() as db:
                    receipt = await db.scalar(select(RunMcpResource))
                    assert receipt is not None and url not in receipt.encrypted_payload and '"session_id"' not in receipt.encrypted_payload
                    if mode == "retired":
                        receipt.retired = True
                    else:
                        row = await db.get(AgentRun, run.id)
                        row.mcp_owner_expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
                        await db.flush()
                        assert await expire_mcp_owners(db) == 1
                if mode == "retry":
                    original = mcp_tools.terminate_mcp_session

                    async def unreachable(*args):
                        """仅控制清理响应，业务请求始终没有再次发送。"""
                        raise TimeoutError("controlled cleanup failure")

                    monkeypatch.setattr(mcp_tools, "terminate_mcp_session", unreachable)
                    assert await cleanup_abandoned_sessions(sessions) == 0
                    async with sessions.begin() as db:
                        pending = await db.get(RunMcpResource, receipt.id)
                        assert pending.next_attempt_at > shanghai_now(naive=True)
                        pending.next_attempt_at = shanghai_now(naive=True) - timedelta(seconds=1)
                    monkeypatch.setattr(mcp_tools, "terminate_mcp_session", original)
                assert await cleanup_abandoned_sessions(sessions) == 1
                async with sessions() as db:
                    assert await db.scalar(select(RunMcpResource)) is None
                assert len(deleted) == 1
                assert sum(item.get("method") == "tools/call" for item in requests) == 1
                assert sum(item.get("method") == "initialize" for item in requests) == 2
            finally:
                await pool.aclose()

    asyncio.run(exercise())


def test_connection_receive_budget_is_cumulative_without_reinitializing(tmp_path, monkeypatch):
    """连续小结果也受同一会话累计字节预算限制，耗尽后不重连清零补做。"""

    async def exercise():
        async with database(tmp_path / "budget.db") as sessions, http_server(monkeypatch) as (url, requests, _, _):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 2048)
            pool = RunMcpSessions(sessions, "budget-worker")
            run = await running(sessions)
            try:
                delivered = 0
                for _ in range(20):
                    try:
                        await call(pool, run, url, schema)
                    except Exception:
                        break
                    delivered += 1
                assert 0 < delivered < 20
                assert sum(item.get("method") == "initialize" for item in requests) == 2
                assert sum(item.get("method") == "tools/call" for item in requests) <= delivered + 1
                await pool.sweep()
                async with sessions() as db:
                    assert (await db.get(AgentRun, run.id)).failure_code == "mcp_session_lost"
            finally:
                await pool.aclose()

    asyncio.run(exercise())


def test_queued_call_rechecks_cancellation_before_sending(tmp_path, monkeypatch):
    """排队时仍获准的调用，在前一调用完成后必须重验取消，不能沿用旧授权发出。"""

    async def exercise():
        async with database(tmp_path / "queued.db") as sessions, http_server(monkeypatch) as (url, requests, _, entered):
            schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            pool = RunMcpSessions(sessions, "queued-worker")
            run = await running(sessions)
            first = second = None
            try:
                first = asyncio.create_task(call(pool, run, url, schema, "slow"))
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(call(pool, run, url, schema, "must-not-send"))
                async with asyncio.timeout(2):
                    # 等到第二项确实通过首次授权并进入连接队列，再撤销运行。
                    while not next(iter(pool.entries.values())).lock._waiters:
                        await asyncio.sleep(0.01)
                async with sessions.begin() as db:
                    await request_cancel(db, run.id, 1)
                entered.release.set()
                await first
                with pytest.raises(ToolNotSentError):
                    await second
                assert sum(item.get("method") == "tools/call" for item in requests) == 1
            finally:
                for task in (first, second):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)
                await pool.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("ending", ["cancel", "owner_lost"])
def test_resource_reaper_preserves_waiting_mcp_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ending: str) -> None:
    """通用回收不误删待审批 MCP 容器和凭据目录，取消或失主后仍回收。"""
    import time
    from app.core.enums import RunState
    from app.services import tool_resources

    async def exercise() -> None:
        """用隔离数据库和受控 Docker 元数据运行真实资源回收逻辑。"""
        root, sandbox = tmp_path / "dependencies", tmp_path / "sandbox"
        monkeypatch.setattr(get_config(), "SCRIPT_DEPENDENCY_STORAGE_DIR", str(root))
        monkeypatch.setattr(get_config(), "SCRIPT_SANDBOX_STORAGE_DIR", str(sandbox))
        directory = sandbox / "mcp-pending"
        directory.mkdir(parents=True)
        removed = []
        async with database(tmp_path / "resources.db") as sessions:
            run = await running(sessions)
            async with sessions.begin() as db:
                await create_approval_request(
                    db,
                    run.id,
                    run.lease_token,
                    "resource-checkpoint",
                    [
                        {
                            "name": "inspect",
                            "arguments_hash": "a" * 64,
                        },
                    ],
                )
                stored = await db.get(AgentRun, run.id)
                stored.mcp_worker_id = "resource-owner"
                stored.mcp_owner_expires_at = shanghai_now(naive=True) + timedelta(seconds=30)
            tool_resources.mark_resource_directory(directory, -int(time.time()), mcp_run_id=run.id)
            labels = {
                "y-point.managed": "tool",
                "y-point.scope": tool_resources.resource_scope(),
                "y-point.deadline": "1",
                "y-point.mcp-run-id": run.id,
            }

            async def docker_metadata(*arguments: str, **kwargs) -> bytes:
                """只替换 Docker IO，审批归属判断和回收决策均用正式实现。"""
                if arguments[:2] == ("ps", "-aq"):
                    return b"aaaaaaaaaaaa" if not removed else b""
                if arguments[0] == "inspect":
                    return json.dumps(
                        [
                            {
                                "Config": {
                                    "Labels": labels,
                                },
                            },
                        ],
                    ).encode()
                if arguments[:2] == ("rm", "-f"):
                    removed.append(arguments[-1])
                    return b"removed"
                return b""

            monkeypatch.setattr(tool_resources, "_docker_metadata", docker_metadata)
            result = await tool_resources.prune_tool_resources(sessions)
            assert result["containers"] == result["directories"] == 0
            assert directory.exists() and not removed
            async with sessions.begin() as db:
                if ending == "cancel":
                    await request_cancel(db, run.id, 1)
                    assert (await db.get(AgentRun, run.id)).state == RunState.CANCELLED
                else:
                    stored = await db.get(AgentRun, run.id)
                    stored.mcp_owner_expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            result = await tool_resources.prune_tool_resources(sessions)
            assert result["containers"] == result["directories"] == 1
            assert not directory.exists() and removed == ["aaaaaaaaaaaa"]

    asyncio.run(exercise())
