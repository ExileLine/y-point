# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:20:41
# @Author  : yangyuexiong
# @File    : test_mcp_connections.py

"""验证共享连接的固定版本、凭据撤销、权限与实际 stdio 调用。"""

import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.middleware import _mask_fields
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.base import Base
from app.models.mcp_connection import McpConnection
from app.models.tool import UserTool, UserToolVersion, RunTool
from app.services import mcp_stdio, mcp_tools
from app.services.mcp_connections import connection_config, require_connection
from app.services.provider_credentials import decrypt_credentials, encrypt_credentials
from app.services.run_manager import create_run
from app.services.user_tools import load_run_tools

PROFILE = {
    "image": "sha256:" + "a" * 64,
    "command": ["python", "/app/server.py"],
    "env_keys": ["API_KEY"],
}
SCHEMA = {
    "type": "object",
    "properties": {
        "value": {
            "type": "string",
        },
    },
    "required": ["value"],
}
SERVER = Path(__file__).parent / "fixtures" / "mcp_stdio_server.py"


@pytest.mark.parametrize("transport,scheme", [("streamable_http", "https"), ("streamable_http", "http"), ("stdio", None)])
def test_shared_connection_api_and_revocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str, scheme: str | None) -> None:
    """两个工具复用连接；普通更新固定旧版，权限和凭据收紧影响已有运行。"""
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "test": PROFILE,
        },
    )
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "1.1.1.1")
    received = []

    async def discover(target, secret):
        """记录实际发现的固定配置，凭据必须以密文传递。"""
        received.append((target, secret))
        return [
            {
                "name": name,
                "input_schema": SCHEMA,
                "read_only": True,
            }
            for name in ("echo", "lookup")
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)
    monkeypatch.setattr("app.api.v1.mcp_connections.discover_mcp_tools", discover)

    async def exercise():
        """通过应用接口与隔离数据库执行连接创建、发布和撤销。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'connections.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="connection-owner", password="unused", role="user")

        async def session_override():
            """为 HTTP 请求提供隔离业务会话。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_current_admin] = lambda: actor
        app.dependency_overrides[get_db_session] = session_override
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(actor)
                db.add(Admin(id=2, username="connection-other", password="unused", role="user"))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                draft = {
                    "name": "shared",
                    "transport": transport,
                    "share_credentials": True,
                }
                draft.update(
                    {
                        "url": f"{scheme}://mcp.example.com:8080/mcp",
                        "bearer_token": "private-first",
                    }
                    if transport == "streamable_http"
                    else {
                        "stdio_profile": "test",
                        "env": {
                            "API_KEY": "private-first",
                        },
                    }
                )
                response = await client.post("/api/v1/mcp-connections", json=draft)
                assert response.status_code == 201, response.text
                connection_id = response.json()["data"]["id"]
                assert "private-first" not in response.text
                assert (await client.post("/api/v1/mcp-connections", json=draft)).status_code == 409
                assert (await client.get("/api/v1/tools/catalog")).json()["data"]["user_tools"] == []
                tool_ids = []
                for name in ("echo", "lookup"):
                    response = await client.post(
                        "/api/v1/tools",
                        json={
                            "name": name,
                            "description": "读取",
                            "kind": "mcp",
                            "visibility": "public",
                            "mcp_connection_id": connection_id,
                            "mcp_tool_name": name,
                        },
                    )
                    assert response.status_code == 201, response.text
                    assert response.json()["data"]["mcp_connection_version"] == 1
                    tool_ids.append(response.json()["data"]["id"])
                catalog = (await client.get("/api/v1/tools/catalog")).json()["data"]["user_tools"]
                assert {item["id"] for item in catalog} == set(tool_ids)
                assert {item["name"] for item in catalog} == {"echo", "lookup"}
                assert len(received) == 2 and all(target["transport"] == transport for target, _ in received)
                token, env = decrypt_credentials(received[0][1])
                assert (token if transport == "streamable_http" else env["API_KEY"]) == "private-first"
                assert (await client.post(f"/api/v1/mcp-connections/{connection_id}/discover")).status_code == 200
                public = (await client.get(f"/api/v1/tools/{tool_ids[0]}")).json()["data"]
                assert public["mcp_connection_id"] == connection_id
                app.dependency_overrides[get_current_admin] = lambda: Admin(id=2, username="connection-other", password="unused")
                assert (await client.get("/api/v1/mcp-connections")).json()["data"] == []
                assert (await client.post(f"/api/v1/mcp-connections/{connection_id}/discover")).status_code == 404
                assert (
                    await client.put(
                        f"/api/v1/mcp-connections/{connection_id}",
                        json={
                            **draft,
                            "expected_version": 1,
                        },
                    )
                ).status_code == 404
                hidden = (await client.get(f"/api/v1/tools/{tool_ids[0]}")).json()["data"]
                assert "mcp_connection_id" not in hidden and "mcp_url" not in hidden
                assert (
                    await client.post(
                        "/api/v1/tools",
                        json={
                            "name": "stolen",
                            "description": "跨用户",
                            "kind": "mcp",
                            "mcp_connection_id": connection_id,
                            "mcp_tool_name": "echo",
                        },
                    )
                ).status_code == 404
                app.dependency_overrides[get_current_admin] = lambda: actor
                # 不提交新凭据，普通更新不能改动已发布 Tool 的连接快照。
                edited = {key: value for key, value in draft.items() if key not in ("bearer_token", "env")}
                if transport == "streamable_http":
                    edited["url"] = "https://next.example.com/mcp"
                else:
                    edited["args"] = ["--new"]
                response = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **edited,
                        "expected_version": 1,
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["data"]["version"] == 2
                assert (
                    await client.put(
                        f"/api/v1/mcp-connections/{connection_id}",
                        json={
                            **edited,
                            "expected_version": 1,
                        },
                    )
                ).status_code == 409
                async with sessions() as db:
                    old = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_ids[0]))
                    assert old.mcp_connection_version == 1
                    assert old.mcp_config.get("url") == draft.get("url")
                    assert old.mcp_config.get("user_args", []) == []
                    assert old.encrypted_credentials and "private-first" not in old.encrypted_credentials
                # 显式发布工具采用连接 v2，仍不影响旧版 Agent 绑定。
                response = await client.put(
                    f"/api/v1/tools/{tool_ids[0]}",
                    json={
                        "expected_version": 1,
                        "mcp_connection_id": connection_id,
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["data"]["mcp_connection_version"] == 2
                async with sessions.begin() as db:
                    run = await create_run(db, actor.id, input_text="执行")
                    version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_ids[0], UserToolVersion.version == 1))
                    db.add(RunTool(run_id=run.id, tool_id=tool_ids[0], tool_version=1, content_hash=version.content_hash))
                    run_id = run.id
                # 撤销共享只阻止其他人使用，不阻止所有者。
                response = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **edited,
                        "share_credentials": False,
                        "expected_version": 2,
                    },
                )
                assert response.status_code == 200, response.text
                async with sessions() as db:
                    tool = await db.get(UserTool, tool_ids[0])
                    version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == 1))
                    await require_connection(db, tool, version, 1)
                    with pytest.raises(CustomException):
                        await require_connection(db, tool, version, 2)
                # 轮换凭据撤销旧快照并取消已排队运行。
                rotated = (
                    {
                        "bearer_token": "private-second",
                    }
                    if transport == "streamable_http"
                    else {
                        "env": {
                            "API_KEY": "private-second",
                        },
                    }
                )
                response = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **edited,
                        **rotated,
                        "expected_version": 3,
                    },
                )
                assert response.status_code == 200, response.text
                assert "private-second" not in response.text
                async with sessions() as db:
                    with pytest.raises(CustomException):
                        await load_run_tools(db, run_id, 1)
                    from app.models.agent_run import AgentRun

                    assert (await db.get(AgentRun, run_id)).state == "cancelled"
                # 保存新 Tool 版本后恢复授权，禁用和删除仍会阻止执行。
                response = await client.put(
                    f"/api/v1/tools/{tool_ids[0]}",
                    json={
                        "expected_version": 2,
                        "mcp_connection_id": connection_id,
                    },
                )
                assert response.status_code == 200, response.text
                response = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **edited,
                        "enabled": False,
                        "expected_version": 4,
                    },
                )
                assert response.status_code == 200, response.text
                assert (await client.post(f"/api/v1/mcp-connections/{connection_id}/discover")).status_code == 409
                assert (await client.delete(f"/api/v1/mcp-connections/{connection_id}")).status_code == 200
                assert (await client.get("/api/v1/mcp-connections")).json()["data"] == []
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["normal", "malformed", "large_output", "large_logs", "nonzero", "stall", "cancel"])
def test_stdio_sdk_transport_and_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """实际 SDK 与子进程交互，验证协议、错误边界、超时及取消清理。"""
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "test": PROFILE,
        },
    )
    monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 4096)
    monkeypatch.setattr(get_config(), "MCP_DISCOVERY_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(mcp_stdio, "SANDBOX_ROOT", tmp_path)
    processes, commands, removed = [], [], []

    async def spawn(command, limit):
        """用受控程序替代 Docker 客户端，真实建立协议管道。"""
        commands.append(command)
        assert command[0:2] == ["docker", "run"]
        assert "--network=none" in command and "--pull=never" in command and "--read-only" in command
        assert not any("private-key" in part for part in command)
        env_path = Path(command[command.index("--env-file") + 1])
        assert env_path.read_text() == "API_KEY=private-key\n"
        assert env_path.stat().st_mode & 0o777 == 0o600
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SERVER),
            "stall" if mode == "cancel" else mode,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=limit + 1,
        )
        processes.append(process)
        return process

    async def remove(name):
        """记录回收操作，不访问真实 Docker。"""
        removed.append(name)

    monkeypatch.setattr(mcp_stdio, "_spawn_stdio", spawn)
    monkeypatch.setattr(mcp_stdio, "_remove_container", remove)

    async def exercise():
        """通过生产发现和调用接口验证换行分帧及响应处理。"""
        config = connection_config(
            {
                "transport": "stdio",
                "stdio_profile": "test",
            }
        )
        secret = encrypt_credentials(
            None,
            {
                "API_KEY": "private-key",
            },
        )
        if mode == "normal":
            found = await mcp_tools.discover_mcp_tools(config, secret)
            assert [tool["name"] for tool in found] == ["echo", "lookup"]
            result = await mcp_tools.invoke_mcp_tool(
                config,
                "echo",
                {
                    "value": "中文内容",
                },
                secret,
                3,
                SCHEMA,
                expected_read_only=True,
            )
            assert "中文内容" in result
            from app.services.tool_arguments import MCPReadToolError

            with pytest.raises(MCPReadToolError):
                await mcp_tools.invoke_mcp_tool(
                    config,
                    "echo",
                    {
                        "value": "error",
                    },
                    secret,
                    3,
                    SCHEMA,
                    expected_read_only=True,
                )
        elif mode == "cancel":
            task = asyncio.create_task(mcp_tools.discover_mcp_tools(config, secret))
            while not processes and not task.done():
                await asyncio.sleep(0)
            if not processes:
                await task
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(Exception) as caught:
                await mcp_tools.discover_mcp_tools(config, secret)
            assert "private-secret" not in str(caught.value)
            assert removed, repr(caught.value)
        assert commands and processes, "场景必须实际启动子进程，不能被配置错误提前阻断"
        assert len(removed) == len(commands) and all(process.returncode is not None for process in processes)
        assert not list(tmp_path.glob("mcp-*"))

    asyncio.run(exercise())


def test_stdio_configuration_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """拒绝未授权镜像与环境变量，并把凭据整体从请求日志中抹除。"""
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "test": PROFILE,
        },
    )
    for fields in [
        {
            "transport": "stdio",
            "stdio_profile": "unknown",
        },
        {
            "transport": "stdio",
            "stdio_profile": "test",
            "env": {
                "LD_PRELOAD": "evil",
            },
        },
        {
            "transport": "stdio",
            "stdio_profile": "test",
            "env": {
                "API_KEY": "x\ny",
            },
        },
        {
            "transport": "stdio",
            "stdio_profile": "test",
            "url": "https://example.com",
        },
        {
            "transport": "streamable_http",
            "url": "https://example.com",
            "stdio_profile": "test",
        },
    ]:
        with pytest.raises(ValueError):
            connection_config(fields)
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "test": {
                **PROFILE,
                "image": "mutable:latest",
            },
        },
    )
    with pytest.raises(ValueError):
        connection_config(
            {
                "transport": "stdio",
                "stdio_profile": "test",
            }
        )
    masked = _mask_fields(
        {
            "env": {
                "API_KEY": "private-key",
            },
            "bearer_token": "private-token",
        },
        set(),
    )
    assert "private-" not in json.dumps(masked)


@pytest.mark.parametrize("transport", ["streamable_http", "stdio"])
def test_worker_uses_pinned_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transport: str) -> None:
    """实际 DeepAgents 图经 Worker 读取固定连接配置，保留审批和调用审计。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage
    from app.services.run_executor import execute_text_run
    from app.services.worker_tools import make_worker_tool_resolver
    from app.models.tool import ToolInvocation

    observed = []
    config = (
        {
            "transport": transport,
            "url": "https://fixed.example.com/mcp",
        }
        if transport == "streamable_http"
        else {
            "transport": transport,
            "stdio_profile": "test",
            "image": PROFILE["image"],
            "command": PROFILE["command"],
            "args": [],
            "env_keys": ["API_KEY"],
        }
    )

    async def invoke(target, name, arguments, secret, timeout, schema, **kwargs):
        """核对传输配置和远端名，不能在运行时跟随连接的普通更新。"""
        observed.append(target)
        assert (
            target == config
            and name == "echo"
            and arguments
            == {
                "value": "真实工具链",
            }
        )
        return "工具执行成功"

    monkeypatch.setattr("app.services.worker_tools.invoke_mcp_tool", invoke)

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            """绑定真正注册的用户 Tool，模型标识与显示名称分别使用。"""
            registered = next(tool for tool in tools if tool.name == "user_tool_3_reader")
            assert "资料读取 Reader" in registered.description
            return self

    async def exercise():
        """在隔离数据库与检查点中运行完整模型、工具及完成事务。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="owner", password="unused"),
                        McpConnection(
                            id=1,
                            owner_id=1,
                            name="shared",
                            version=2,
                            config={
                                **config,
                                "url": "https://new.example.com/mcp",
                            },
                        ),
                        UserTool(
                            id=3,
                            owner_id=1,
                            name="reader",
                            display_name="资料读取 Reader",
                            description="读取",
                            kind="mcp",
                            visibility="private",
                            enabled=True,
                        ),
                        UserToolVersion(
                            tool_id=3,
                            version=1,
                            content_hash="a" * 64,
                            input_schema=SCHEMA,
                            effect="read",
                            recovery_policy="safe",
                            requires_approval=False,
                            verified=True,
                            mcp_connection_id=1,
                            mcp_connection_version=1,
                            mcp_config=config,
                            mcp_tool_name="echo",
                        ),
                    ]
                )
                run = await create_run(db, 1, input_text="读取资料")
                db.add(RunTool(run_id=run.id, tool_id=3, tool_version=1, content_hash="a" * 64, requires_approval=False))
            model = Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "user_tool_3_reader",
                                "args": {
                                    "value": "真实工具链",
                                },
                                "id": "echo-call",
                            }
                        ],
                    ),
                    AIMessage(content="已完成读取"),
                ]
            )
            result = await execute_text_run(
                sessions,
                run.id,
                model,
                tmp_path / "checkpoints.db",
                resolve_tools=make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=0.05,
            )
            assert result.state == "completed", result.failure_code
            async with sessions() as db:
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
                assert invocation.state == "completed"
                from app.models.agent_run_event import AgentRunEvent
                from app.core.enums import RunEventType
                from app.services.run_calls import load_run_calls

                event = await db.scalar(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id, AgentRunEvent.event_type == RunEventType.TOOL_STARTED))
                assert event.tool_name == "user_tool_3_reader"
                assert event.details["display_name"] == "资料读取 Reader"
                definition = await db.get(UserTool, 3)
                definition.display_name = "运行后修改的名称"
                await db.commit()
                summary = await load_run_calls(db, 1, [run.id])
                assert next(call for call in summary[run.id] if call.call_id == event.tool_call_id).name == "资料读取 Reader"
                definition.name = "_deleted_3"
                definition.display_name = None
                definition.is_deleted = 1
                await db.commit()
                deleted_summary = await load_run_calls(db, 1, [run.id])
                recorded = next(call for call in deleted_summary[run.id] if call.call_id == event.tool_call_id)
                assert recorded.name == "资料读取 Reader" and recorded.kind == "mcp"
            assert observed == [config]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["normal", "malformed", "large_output", "large_logs", "nonzero", "stall", "cancel"])
def test_stdio_real_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """可选真实容器验收，镜像只含受控标准库 Server，不访问真实上游。"""
    import os
    import subprocess

    image = os.environ.get("YPOINT_TEST_MCP_STDIO_IMAGE")
    if not image:
        pytest.skip("需指定包含 tests/fixtures/mcp_stdio_server.py 的固定镜像 ID")
    profile = {
        **PROFILE,
        "image": image,
    }
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "docker_test": profile,
        },
    )
    monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 4096)
    monkeypatch.setattr(get_config(), "MCP_DISCOVERY_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(mcp_stdio, "SANDBOX_ROOT", tmp_path)

    def containers():
        """只比较本次测试涉及的 MCP 容器，不处理其他工作负载。"""
        return set(subprocess.check_output(["docker", "ps", "-a", "--filter", "name=yp-mcp-", "--format", "{{.Names}}"], text=True).splitlines())

    async def exercise():
        """通过生产容器传输验证发现、调用和失败后回收。"""
        config = connection_config(
            {
                "transport": "stdio",
                "stdio_profile": "docker_test",
                "args": ["stall" if mode == "cancel" else mode],
            }
        )
        before = containers()
        if mode == "normal":
            tools = await mcp_tools.discover_mcp_tools(config)
            assert {tool["name"] for tool in tools} == {"echo", "lookup"}
            result = await mcp_tools.invoke_mcp_tool(
                config,
                "echo",
                {
                    "value": "Docker 真实调用",
                },
                None,
                5,
                SCHEMA,
                expected_read_only=True,
            )
            assert "Docker 真实调用" in result
        elif mode == "cancel":
            task = asyncio.create_task(mcp_tools.discover_mcp_tools(config))
            await asyncio.sleep(0.4)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(Exception):
                await mcp_tools.discover_mcp_tools(config)
        assert containers() == before
        assert not list(tmp_path.glob("mcp-*"))

    asyncio.run(exercise())


def test_connection_revocation_only_cancels_matching_run_versions(tmp_path: Path) -> None:
    """同一 Tool 改绑连接后，撤销旧连接不能取消已采用新连接的运行。"""
    from app.models.agent_run import AgentRun
    from app.services.mcp_connections import cancel_connection_runs

    async def exercise():
        """按 Run 固定的 Tool 版本筛选，分别验证所有者及借用者的取消范围。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel-scope.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="owner", password="unused"),
                        Admin(id=2, username="borrower", password="unused"),
                        McpConnection(
                            id=1,
                            owner_id=1,
                            name="old",
                            config={
                                "transport": "stdio",
                            },
                        ),
                        McpConnection(
                            id=2,
                            owner_id=1,
                            name="new",
                            config={
                                "transport": "stdio",
                            },
                        ),
                        UserTool(id=3, owner_id=1, name="reader", description="读取", kind="mcp", visibility="public", enabled=True, current_version=2),
                    ]
                )
                ids = []
                for owner, version in [(1, 1), (1, 2), (2, 1)]:
                    run = await create_run(db, owner, input_text="读取资料")
                    ids.append(run.id)
                    db.add(RunTool(run_id=run.id, tool_id=3, tool_version=version, content_hash="a" * 64))
                for version in (1, 2):
                    db.add(
                        UserToolVersion(
                            tool_id=3,
                            version=version,
                            content_hash="a" * 64,
                            input_schema=SCHEMA,
                            effect="read",
                            requires_approval=False,
                            mcp_connection_id=version,
                            mcp_connection_version=1,
                        )
                    )
            async with sessions.begin() as db:
                old = await db.get(McpConnection, 1)
                await cancel_connection_runs(db, old, others_only=True)
            async with sessions() as db:
                assert [(await db.get(AgentRun, id_)).state for id_ in ids] == ["queued", "queued", "cancelled"]
            async with sessions.begin() as db:
                await cancel_connection_runs(db, await db.get(McpConnection, 1))
            async with sessions() as db:
                assert [(await db.get(AgentRun, id_)).state for id_ in ids] == ["cancelled", "queued", "cancelled"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
