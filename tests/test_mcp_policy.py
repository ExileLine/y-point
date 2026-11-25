# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 10:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_policy.py

"""验证 MCP 分层审批、只读证据和安全可调用状态的统一契约。"""

import asyncio
from contextlib import asynccontextmanager
import json
from datetime import datetime
from pathlib import Path

import httpx
from mcp.types import ListToolsResult, Tool, ToolAnnotations
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.base import Base
from app.models.mcp_connection import McpConnection
from app.models.tool import UserTool, UserToolVersion
from app.services import mcp_tools
from app.services.mcp_policy import approval_policy
from app.services.tool_delivery import ToolNotSentError


@pytest.mark.parametrize(
    "effect,source,connection_mode,override,actor_id,verified,require_all,expected",
    [
        ("read", "remote_read", "writes", "inherit", 1, False, False, False),
        ("read", "remote_read", "always", "inherit", 1, False, False, True),
        ("read", "remote_read", "always", "writes", 1, False, False, False),
        ("read", "remote_read", "never", "always", 1, False, False, True),
        ("write", "remote_write", "never", "never", 1, False, False, False),
        ("write", "unknown", "writes", "inherit", 1, False, False, True),
        ("write", "remote_write", "writes", "inherit", 1, True, False, True),
        ("write", "remote_write", "always", "never", 1, True, False, False),
        ("write", "remote_write", "never", "always", 1, True, False, True),
        ("read", "remote_read", "never", "inherit", 1, False, True, True),
        ("read", "owner_read", "writes", "inherit", 1, False, False, False),
        ("read", "owner_read", "never", "never", 2, False, False, True),
        ("read", "remote_read", "writes", "inherit", 2, False, False, False),
        ("read", None, "writes", "inherit", 2, False, False, True),
        ("write", "unknown", "never", "inherit", 1, False, False, False),
        ("write", "unknown", "never", "inherit", 2, False, False, True),
        ("write", "remote_write", "never", "never", 1, False, True, True),
    ],
)
def test_mcp_policy_decision_table(monkeypatch, effect, source, connection_mode, override, actor_id, verified, require_all, expected) -> None:
    """覆盖所有者审批配置、共享使用与 Run 收紧，幂等恢复不覆盖审批方式。"""
    monkeypatch.setattr(get_config(), "MCP_FORCE_APPROVAL", False)
    tool = UserTool(id=1, owner_id=1, kind="mcp")
    version = UserToolVersion(
        version=1,
        effect=effect,
        effect_source=source,
        approval_mode=override,
        requires_approval=False,
        recovery_policy="idempotent" if verified else "manual",
        idempotency_key_field="request_id" if verified else None,
        write_contract_verified=verified,
    )
    connection = McpConnection(version=2, approval_mode=connection_mode)
    decision = approval_policy(tool, version, actor_id, connection=connection, require_all=require_all)
    assert decision["requires_approval"] is expected
    assert decision["source"] == ("connection" if override == "inherit" else "tool")
    monkeypatch.setattr(get_config(), "MCP_FORCE_APPROVAL", True)
    forced = approval_policy(tool, version, actor_id, connection=connection)
    assert forced["requires_approval"] is True and "deployment_requires_approval" in forced["constraints"]


async def assert_callability_entries(client: httpx.AsyncClient, tool_id: int, expected: str, connection_id: int | None = None) -> dict:
    """正式目录、详情与连接分组在相同身份下必须返回相同语义和独立判断时间。"""
    detail_response = await client.get(f"/api/v1/tools/{tool_id}")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()["data"]
    catalog_response = await client.get("/api/v1/tools/catalog")
    catalog = next(item for item in catalog_response.json()["data"]["user_tools"] if item["id"] == tool_id)
    entries = [detail, catalog]
    if connection_id is not None:
        directory_response = await client.get("/api/v1/mcp-connections/catalog")
        connection = next(item for item in directory_response.json()["data"] if item["id"] == connection_id)
        assert isinstance(connection["version"], int)
        assert connection["can_edit"] is True and connection["share_version"] is None
        entries.append(next(item for item in connection["tools"] if item["tool_id"] == tool_id))
    for item in entries:
        assert item["callability"] == detail["callability"]
        assert item["callability"]["status"] == expected
        assert datetime.fromisoformat(item["callability_checked_at"]).utcoffset() is not None
        assert item["mcp_connection_version"] == detail["mcp_connection_version"]
    return detail


def test_read_confirmation_versions_sharing_and_callability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """通过真实 HTTP 路由验证未知提示、私有确认、共享收紧及定义变化重确认。"""
    remote = {
        "name": "lookup",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "read_only": None,
    }

    async def discover(*args, **kwargs):
        """返回可控制的远端声明，测试中不连接外部服务。"""
        return [remote]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "127.0.0.1")

    async def exercise() -> None:
        """私有库中执行真实接口，保留版本历史和按使用者过滤的目录。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp-policy.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actors = {
            1: Admin(id=1, username="owner", password="unused", role="user", status=1, is_deleted=0),
            2: Admin(id=2, username="consumer", password="unused", role="user", status=1, is_deleted=0),
        }
        actor_id = 1

        async def session_override():
            """向接口提供独立数据库会话。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db_session] = session_override
        app.dependency_overrides[get_current_admin] = lambda: actors[actor_id]
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(actors.values())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/api/v1/tools",
                    json={
                        "name": "unknown_lookup",
                        "description": "未知读写声明的查询",
                        "kind": "mcp",
                        "mcp_url": "https://private.invalid/mcp",
                        "mcp_tool_name": "lookup",
                    },
                )
                assert response.status_code == 201, response.text
                tool = response.json()["data"]
                tool_id = tool["id"]
                assert (await assert_callability_entries(client, tool_id, "approval_required"))["callability"] == tool["callability"]
                assert tool["effect"] == "write" and tool["effect_source"] == "unknown"
                assert tool["requires_approval"] is True and tool["callability"]["status"] == "approval_required"
                for changes in [
                    {
                        "effect": "read",
                    },
                    {
                        "effect": "read",
                        "read_only_reason": "   ",
                    },
                ]:
                    denied = await client.put(
                        f"/api/v1/tools/{tool_id}",
                        json={
                            "expected_version": 1,
                            **changes,
                        },
                    )
                    assert denied.status_code == 400, denied.text
                confirmed = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 1,
                        "effect": "read",
                        "read_only_reason": "已核对源代码，仅查询数据",
                    },
                )
                assert confirmed.status_code == 200, confirmed.text
                data = confirmed.json()["data"]
                assert (await assert_callability_entries(client, tool_id, "direct"))["callability"] == data["callability"]
                evidence = data["read_confirmation"]
                assert data["effect_source"] == "owner_read" and data["recovery_policy"] == "safe"
                assert data["callability"]["status"] == "direct" and evidence["actor_id"] == 1
                renamed = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 2,
                        "display_name": "查询工具（新名称）",
                        "visibility": "public",
                    },
                )
                assert renamed.status_code == 200, renamed.text
                assert renamed.json()["data"]["read_confirmation"] == evidence
                actor_id = 2
                shared = await client.get(f"/api/v1/tools/{tool_id}")
                catalog = await client.get("/api/v1/tools/catalog")
                await assert_callability_entries(client, tool_id, "approval_required")
                summary = shared.json()["data"]
                assert summary["callability"]["status"] == "approval_required"
                assert "untrusted_read_declaration" in summary["callability"]["policy"]["constraints"]
                assert "mcp_url" not in summary and "read_confirmation" not in summary
                assert "private.invalid" not in json.dumps(catalog.json())
                assert catalog.json()["data"]["user_tools"][0]["callability"] == summary["callability"]
                actor_id = 1
                remote["input_schema"]["properties"]["scope"] = {
                    "type": "string",
                }
                changed = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 3,
                        "description": "远端定义已变化",
                    },
                )
                assert changed.status_code == 400 and "重新确认" in changed.text
                reconfirmed = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 3,
                        "visibility": "private",
                        "read_only_reason": "已检查新增 scope 参数仍为查询",
                    },
                )
                assert reconfirmed.status_code == 200, reconfirmed.text
                assert reconfirmed.json()["data"]["read_confirmation"]["definition_hash"] != evidence["definition_hash"]
                remote["read_only"] = False
                forbidden = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 4,
                        "effect": "read",
                        "read_only_reason": "尝试降级",
                    },
                )
                assert forbidden.status_code == 400 and "不能" in forbidden.text
                # 按连接继承的策略通过正式接口收紧，并保留旧 Run 的审批事实。
                from app.models.agent_config import AgentConfig
                from app.models.agent_run import AgentRun
                from app.models.tool import AgentTool, RunTool
                from app.services.run_manager import create_run
                from app.services.user_tools import snapshot_run_tools

                remote["read_only"] = True
                connection_draft = {
                    "name": "私有查询连接",
                    "transport": "streamable_http",
                    "url": "https://private.invalid/mcp",
                }
                connection_response = await client.post("/api/v1/mcp-connections", json=connection_draft)
                assert connection_response.status_code == 201, connection_response.text
                connection_id = connection_response.json()["data"]["id"]
                assert connection_response.json()["data"]["approval_mode"] == "writes"
                linked_response = await client.post(
                    "/api/v1/tools",
                    json={
                        "name": "linked_lookup",
                        "description": "连接默认审批验证",
                        "kind": "mcp",
                        "mcp_connection_id": connection_id,
                        "mcp_tool_name": "lookup",
                    },
                )
                assert linked_response.status_code == 201, linked_response.text
                linked_id = linked_response.json()["data"]["id"]
                assert linked_response.json()["data"]["callability"]["status"] == "direct"
                assert (await assert_callability_entries(client, linked_id, "direct", connection_id))["mcp_connection_version"] == 1
                async with sessions.begin() as db:
                    db.add(AgentConfig(id=10, owner_id=1, name="查询助手", default_model_id=1))
                    db.add(AgentTool(agent_id=10, tool_id=linked_id, tool_version=1))
                    old_run = await create_run(db, 1, agent_id=10, input_text="免审批查询")
                    await snapshot_run_tools(db, old_run.id, 10, 1)
                tightened = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **connection_draft,
                        "expected_version": 1,
                        "approval_mode": "always",
                    },
                )
                assert tightened.status_code == 200, tightened.text
                async with sessions.begin() as db:
                    assert (await db.get(AgentRun, old_run.id)).state == "cancelled"
                    fixed = await db.scalar(select(RunTool).where(RunTool.run_id == old_run.id))
                    assert fixed.requires_approval is False
                    assert fixed.approval_policy["mode"] == "writes" and fixed.approval_policy["connection_policy_version"] == 1
                    caller_run = await create_run(db, 1, agent_id=10, input_text="调用者收紧", require_mcp_approval=True)
                    await snapshot_run_tools(db, caller_run.id, 10, 1)
                legacy_update = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **connection_draft,
                        "expected_version": 2,
                    },
                )
                assert legacy_update.status_code == 200 and legacy_update.json()["data"]["approval_mode"] == "always"
                latest_tool = await client.get(f"/api/v1/tools/{linked_id}")
                assert latest_tool.json()["data"]["callability"]["status"] == "approval_required"
                checked = await assert_callability_entries(client, linked_id, "approval_required", connection_id)
                assert checked["mcp_connection_version"] == 1
                assert checked["callability"]["policy"]["connection_policy_version"] == 3
                relaxed = await client.put(
                    f"/api/v1/mcp-connections/{connection_id}",
                    json={
                        **connection_draft,
                        "expected_version": 3,
                        "approval_mode": "writes",
                    },
                )
                assert relaxed.status_code == 200, relaxed.text
                async with sessions() as db:
                    assert (await db.get(AgentRun, caller_run.id)).state == "queued"
                    fixed = await db.scalar(select(RunTool).where(RunTool.run_id == caller_run.id))
                    assert fixed.requires_approval is True and "caller_requires_approval" in fixed.approval_policy["constraints"]
                disabled = await client.put(
                    f"/api/v1/tools/{linked_id}",
                    json={
                        "expected_version": 1,
                        "enabled": False,
                    },
                )
                assert disabled.status_code == 200, disabled.text
                blocked = await assert_callability_entries(client, linked_id, "blocked", connection_id)
                assert blocked["callability"]["reason"] == "disabled"
                assert blocked["callability"] == disabled.json()["data"]["callability"]
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_current_admin, None)
            await engine.dispose()

    asyncio.run(exercise())


def test_discovery_preserves_missing_hint_and_preflight_never_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """SDK 注解对象的默认 false 不冒充远端显式声明；预检失败保留未发送证据。"""
    sent = []

    class Session:
        """模拟协议握手和发现，在 tools/call 的发送处计数。"""

        def __init__(self, *args, **kwargs):
            """不创建网络资源。"""

        async def __aenter__(self):
            """返回当前会话。"""
            return self

        async def __aexit__(self, *args):
            """退出可控会话。"""

        async def initialize(self):
            """握手成功后仍须检查输入定义。"""

        async def list_tools(self, **kwargs):
            """仅提供幂等提示，没有显式只读提示。"""
            return ListToolsResult(
                tools=[
                    Tool(
                        name="lookup",
                        inputSchema={
                            "type": "object",
                        },
                        annotations=ToolAnnotations(idempotentHint=True),
                    )
                ]
            )

        async def call_tool(self, *args, **kwargs):
            """若预检正确，此方法不可被调用。"""
            sent.append(True)
            raise AssertionError("tools/call 不应发送")

    @asynccontextmanager
    async def connection(*args, **kwargs):
        """替代传输，不访问网络。"""
        yield None, None, None

    monkeypatch.setattr(mcp_tools, "ClientSession", Session)
    monkeypatch.setattr(mcp_tools, "_mcp_connection", connection)

    async def exercise() -> None:
        """通过正式发现和调用函数验证提示与业务请求发送状态。"""
        discovered = await mcp_tools.discover_mcp_tools("https://private.invalid/mcp", None)
        assert discovered[0]["read_only"] is None and discovered[0]["annotations"] == {
            "idempotentHint": True,
        }
        with pytest.raises(ToolNotSentError):
            await mcp_tools.invoke_mcp_tool(
                "https://private.invalid/mcp",
                "lookup",
                {},
                None,
                10,
                {
                    "type": "object",
                    "required": ["changed"],
                },
            )
        with pytest.raises(ToolNotSentError):
            await mcp_tools.invoke_mcp_tool(
                "https://private.invalid/mcp",
                "lookup",
                {},
                None,
                10,
                {
                    "type": "object",
                },
                expected_read_only=True,
                expected_annotations={
                    "readOnlyHint": True,
                },
            )
        assert sent == []

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["success", "schema_changed", "response_lost"])
def test_official_mcp_http_protocol_preserves_delivery_evidence(monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """真实 SDK 与 ASGI MCP 服务联调，在业务响应阶段注入断线而不访问外部服务。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    from app.services.tool_delivery import request_not_sent

    server = FastMCP(
        "ticket-delivery",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    calls = []

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False))
    async def create_ticket(title: str) -> str:
        """模拟创建工单，在远端实际执行点计数。"""
        calls.append(title)
        return "T-1"

    application = server.streamable_http_app()

    async def unreliable(scope, receive, send):
        """解析实际协议请求，在写入完成后的响应传输中注入异常。"""
        body = bytearray()
        method = None

        async def observed_receive():
            """旁路记录请求类型，完整转交原始请求流。"""
            nonlocal method
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if body and not message.get("more_body", False):
                    method = json.loads(body).get("method")
            return message

        async def observed_send(message):
            """只在工具结果返回时断线，握手和发现保持正常。"""
            if outcome == "response_lost" and method == "tools/call" and message["type"] == "http.response.body":
                assert len(calls) == 1
                raise httpx.ReadError("controlled response loss")
            await send(message)

        await application(scope, observed_receive, observed_send)

    def initialize_transport(self, host, address, *, scheme="https", port=None):
        """替换最底层 HTTP 网络，保持固定目标检查、接收预算与协议代码。"""
        self.host, self.address, self.scheme = host, address, scheme
        self.port = port or 443
        self.received = 0
        self.max_bytes = get_config().MCP_RESPONSE_MAX_BYTES
        self.transport = httpx.ASGITransport(app=unreliable)

    monkeypatch.setattr(mcp_tools._PinnedMcpTransport, "__init__", initialize_transport)
    monkeypatch.setattr(mcp_tools, "validate_mcp_url", lambda url: "127.0.0.1")

    async def exercise() -> None:
        """正式 SDK 执行发现与调用，证明预检失败和发送后断线可被区分。"""
        async with server.session_manager.run():
            remote = (await mcp_tools.discover_mcp_tools("https://mcp.invalid/mcp", None))[0]
            schema = dict(remote["input_schema"])
            if outcome == "schema_changed":
                schema["required"] = ["unavailable"]
            if outcome == "success":
                result = await mcp_tools.invoke_mcp_tool(
                    "https://mcp.invalid/mcp",
                    "create_ticket",
                    {
                        "title": "登录问题",
                    },
                    None,
                    5,
                    schema,
                )
                assert "T-1" in result and calls == ["登录问题"]
            else:
                with pytest.raises(Exception) as failure:
                    await mcp_tools.invoke_mcp_tool(
                        "https://mcp.invalid/mcp",
                        "create_ticket",
                        {
                            "title": "登录问题",
                        },
                        None,
                        5,
                        schema,
                    )
                assert request_not_sent(failure.value) is (outcome == "schema_changed")
                assert calls == ([] if outcome == "schema_changed" else ["登录问题"])

    asyncio.run(exercise())


def test_legacy_run_keeps_stronger_approval_when_current_policy_relaxes(tmp_path: Path) -> None:
    """旧快照为空时不因当前版本放宽而自动批准，也不被无关更新误取消。"""
    from app.models.agent_run import AgentRun
    from app.models.tool import RunTool
    from app.services.mcp_policy import cancel_tightened_tool_runs
    from app.services.run_manager import claim_run, create_run
    from app.services.worker_tools import make_worker_tool_resolver

    async def exercise() -> None:
        """真实解析固定旧版本及当前发布策略，验证历史待审批语义。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-policy.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="legacy-owner", password="unused", role="user", status=1, is_deleted=0))
                db.add(UserTool(id=1, owner_id=1, name="lookup", description="旧版本查询", kind="mcp", visibility="private", enabled=True, current_version=2))
                for number, required in [(1, True), (2, False)]:
                    db.add(
                        UserToolVersion(
                            tool_id=1,
                            version=number,
                            input_schema={
                                "type": "object",
                            },
                            content_hash=("a" if number == 1 else "b") * 64,
                            effect="read",
                            requires_approval=required,
                            approval_mode=None if number == 1 else "writes",
                            verified=True,
                            mcp_url="https://mcp.invalid/mcp",
                            mcp_tool_name="lookup",
                        ),
                    )
                run = await create_run(db, 1, input_text="查询")
                db.add(RunTool(run_id=run.id, tool_id=1, tool_version=1, content_hash="a" * 64))
            async with sessions.begin() as db:
                run = await claim_run(db, run.id)
                _, approvals = await make_worker_tool_resolver(sessions)(db, run)
                assert approvals["user_tool_1_lookup"] is True
                await cancel_tightened_tool_runs(db, tool_id=1)
                assert (await db.get(AgentRun, run.id)).state == "running"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_outer_timeout_keeps_preflight_not_sent_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """正式执行器的 wait_for 在握手阶段取消时，未发送证据不会丢失。"""
    from app.services.tool_delivery import request_not_sent

    class SlowSession:
        """在握手处等待取消，不能发送业务请求。"""

        def __init__(self, *args, **kwargs):
            """不创建网络资源。"""

        async def __aenter__(self):
            """提供可控会话。"""
            return self

        async def __aexit__(self, *args):
            """结束握手会话。"""

        async def initialize(self):
            """等待外部单次工具时限触发取消。"""
            await asyncio.Event().wait()

    @asynccontextmanager
    async def connection(*args, **kwargs):
        """提供可控传输占位。"""
        yield None, None, None

    monkeypatch.setattr(mcp_tools, "ClientSession", SlowSession)
    monkeypatch.setattr(mcp_tools, "_mcp_connection", connection)

    async def exercise() -> None:
        """经过正式 MCP 调用及外层 wait_for 检查异常链。"""
        with pytest.raises(TimeoutError) as failure:
            await asyncio.wait_for(
                mcp_tools.invoke_mcp_tool(
                    "https://mcp.invalid/mcp",
                    "lookup",
                    {},
                    None,
                    10,
                    {
                        "type": "object",
                    },
                ),
                timeout=0.01,
            )
        assert request_not_sent(failure.value)

    asyncio.run(exercise())
