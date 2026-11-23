# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 15:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_auth_sync.py

"""验证 MCP 鉴权、同步发布、Agent 显式升级与旧运行隔离。"""

import asyncio
import copy
from pathlib import Path

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.middleware import _mask_fields
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.tool import AgentTool, RunTool, UserTool, UserToolVersion
from app.services.mcp_auth import assemble_headers, auth_config, http_credentials, normalize_headers
from app.services.provider_credentials import decrypt_credentials
from app.services.run_manager import create_run
from app.services.user_tools import model_tool_name, snapshot_run_tools

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
        },
    },
    "required": ["title"],
}


def remote(name: str, *, read: bool | None = True) -> dict:
    """创建显式注解或缺少注解的协议目录。"""
    return {
        "name": name,
        "description": "查询服务",
        "input_schema": copy.deepcopy(SCHEMA),
        "read_only": read,
        "annotations": {}
        if read is None
        else {
            "readOnlyHint": read,
        },
    }


@pytest.mark.parametrize(
    "headers",
    [
        {
            "Host": "wrong",
        },
        {
            "Authorization": "secret",
        },
        {
            "Mcp-Session-Id": "wrong",
        },
        {
            "Accept": "wrong",
        },
        {
            "Content-Type": "wrong",
        },
        {
            "X-Forwarded-Host": "wrong",
        },
        {
            "Cookie": "wrong",
        },
        {
            "X-A": "1",
            "x-a": "2",
        },
        {
            "X-A": "line\nbreak",
        },
        {
            "X-A": "中文",
        },
        {
            "X-A": "a" * 4097,
        },
        {f"X-{n}": "v" for n in range(17)},
    ],
)
def test_mcp_header_rejects_protocol_override_injection_and_limits(headers) -> None:
    """HTTP 头不能覆盖 MCP/HTTP 协议或绕过大小写、字符及数量限制。"""
    with pytest.raises(ValueError):
        normalize_headers(headers)


def test_mcp_auth_update_preserves_replaces_clears_and_masks(monkeypatch) -> None:
    """凭据加密、空值保留、敏感字段逐项删除、显式清除与日志脱敏。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    data = {
        "auth_type": "api_key",
        "api_key_header": "X-API-Key",
        "api_key": "private-key",
        "headers": {
            "X-Tenant": "tenant-a",
        },
        "sensitive_headers": {
            "X-Secret": "private-header",
        },
    }
    for mode in ("bearer", "api_key"):
        incomplete = auth_config(
            {
                "auth_type": mode,
            }
        )
        assert http_credentials({}, incomplete) is None
        with pytest.raises(ValueError, match="尚未配置"):
            assemble_headers(incomplete, None)
    config = auth_config(data)
    secret = http_credentials(data, config)
    assert "private-key" not in secret and "private-header" not in secret
    assert assemble_headers(config, secret) == {
        "x-api-key": "private-key",
        "x-tenant": "tenant-a",
        "x-secret": "private-header",
    }
    assert http_credentials({}, config, secret, config) == secret
    removed = http_credentials(
        {
            "sensitive_headers": {
                "X-Secret": None,
            }
        },
        config,
        secret,
        config,
    )
    assert decrypt_credentials(removed) == ("private-key", {})
    cleared = http_credentials(
        {
            "clear_credentials": True,
        },
        config,
        secret,
        config,
    )
    assert cleared is None
    with pytest.raises(ValueError, match="尚未配置"):
        assemble_headers(config, cleared)
    with pytest.raises(ValueError, match="冲突"):
        http_credentials(
            {
                "sensitive_headers": {
                    "x-tenant": "private",
                }
            },
            config,
            secret,
            config,
        )
    with pytest.raises(ValueError, match="冲突"):
        http_credentials(
            {
                "sensitive_headers": {
                    "x-api-key": "private",
                }
            },
            config,
            secret,
            config,
        )
    assert "private-header" not in str(_mask_fields(data, set()))
    with pytest.raises(ValueError, match="16 KiB"):
        normalize = {f"X-{n}": "a" * 4096 for n in range(5)}
        assemble_headers(
            auth_config(
                {
                    "headers": normalize,
                }
            ),
            None,
        )


@pytest.mark.parametrize("mode", ["none", "bearer", "api_key"])
def test_mcp_official_protocol_shares_auth_for_discovery_and_call(monkeypatch, mode) -> None:
    """官方 SDK 发出的握手、发现及正式调用均携带同一租户和鉴权。"""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations
    from app.services import mcp_tools

    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    server = FastMCP("headers", stateless_http=True, json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    calls = []

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def search(title: str) -> str:
        """在受控服务中观察业务调用。"""
        calls.append(title)
        return "found"

    application = server.streamable_http_app()
    observed = []

    async def observe(scope, receive, send):
        """保留实际协议，仅检查进入远端的请求头。"""
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            assert headers[b"x-tenant"] == b"tenant-a" and headers[b"x-secret"] == b"private-header"
            if mode == "bearer":
                assert headers[b"authorization"] == b"Bearer private-key"
            elif mode == "api_key":
                assert headers[b"x-custom-key"] == b"private-key"
            else:
                assert b"authorization" not in headers and b"x-custom-key" not in headers
            observed.append(scope["method"])
        await application(scope, receive, send)

    def transport(self, host, address, *, scheme="https", port=None):
        """仅替换网络，保留固定目标检查、请求头组装及真实 SDK。"""
        self.host, self.address, self.scheme, self.port = host, address, scheme, port or 443
        self.received, self.max_bytes = 0, get_config().MCP_RESPONSE_MAX_BYTES
        self.transport = httpx.ASGITransport(app=observe)

    monkeypatch.setattr(mcp_tools._PinnedMcpTransport, "__init__", transport)
    monkeypatch.setattr(mcp_tools, "validate_mcp_url", lambda url: "127.0.0.1")
    data = {
        "auth_type": mode,
        "api_key_header": "X-Custom-Key",
        "headers": {
            "X-Tenant": "tenant-a",
        },
        "sensitive_headers": {
            "X-Secret": "private-header",
        },
    }
    if mode != "none":
        data["bearer_token" if mode == "bearer" else "api_key"] = "private-key"
    config = {
        "transport": "streamable_http",
        "url": "https://mcp.invalid/mcp",
        **auth_config(data),
    }
    secret = http_credentials(data, config)

    async def exercise():
        """通过正式发现和执行入口证明配置一致。"""
        async with server.session_manager.run():
            found = await mcp_tools.discover_mcp_tools(config, secret)
            await mcp_tools.invoke_mcp_tool(
                config,
                "search",
                {
                    "title": "test",
                },
                secret,
                10,
                found[0]["input_schema"],
                expected_read_only=True,
            )
        assert calls == ["test"] and len(observed) >= 6

    asyncio.run(exercise())


def test_sync_publish_upgrade_idempotency_and_revocation(tmp_path: Path, monkeypatch) -> None:
    """实际接口贯穿差异、部分发布、重试、Agent 选择升级、租户撤销与旧快照。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "127.0.0.1")
    definitions = [remote("search"), remote("unknown", read=None)]
    discoveries = []

    async def discover(config, secret):
        """远端目录可以演进，检查发现始终使用保存的秘密与租户。"""
        discoveries.append(assemble_headers(config, secret))
        return copy.deepcopy(definitions)

    monkeypatch.setattr("app.api.v1.mcp_connections.discover_mcp_tools", discover)
    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def exercise():
        """每次 HTTP 请求都使用真实 SQLAlchemy 事务，隔离于业务数据。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="sync-owner", password="unused", role="user")

        async def database():
            """为 API 提供独立业务事务。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_current_admin] = lambda: actor
        app.dependency_overrides[get_db_session] = database
        try:
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(actor)
                db.add(ProviderConnection(id=1, creator_id=1, name="mock", name_key="mock", kind="openai", base_url="https://model.invalid/v1"))
                db.add(
                    ModelCatalog(
                        id=1,
                        creator_id=1,
                        provider_id=1,
                        external_id="model",
                        display_name="测试模型",
                        capabilities=["text", "tools"],
                        verified_capabilities=["text", "tools"],
                    )
                )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                draft = {
                    "name": "服务 A",
                    "transport": "streamable_http",
                    "url": "http://mcp.test/mcp",
                    "auth_type": "api_key",
                    "api_key_header": "X-Key",
                    "api_key": "private-key",
                    "headers": {
                        "X-Tenant": "tenant-a",
                    },
                    "sensitive_headers": {
                        "X-Secret": "private-header",
                    },
                }
                saved = await client.post("/api/v1/mcp-connections", json=draft)
                assert saved.status_code == 201, saved.text
                connection = saved.json()["data"]
                cid = connection["id"]
                assert connection["header_names"] == ["x-tenant"] and connection["sensitive_header_names"] == ["x-secret"]
                assert "tenant-a" not in saved.text and "private-key" not in saved.text and "private-header" not in saved.text

                async def preview():
                    """每次发布都先获取对应目录与连接版本的预览。"""
                    response = await client.post(f"/api/v1/mcp-connections/{cid}/sync-preview")
                    assert response.status_code == 200, response.text
                    return response.json()["data"]

                async def apply(plan, request_id, selections):
                    """提交明确子集并保留可重试请求。"""
                    body = {
                        "request_id": request_id,
                        "expected_version": plan["connection_version"],
                        "preview_hash": plan["preview_hash"],
                        "selections": selections,
                    }
                    response = await client.post(f"/api/v1/mcp-connections/{cid}/sync", json=body)
                    assert response.status_code == 200, response.text
                    return body, response.json()["data"]

                first = await preview()
                assert [row["state"] for row in first["items"]] == ["added", "added"]
                async with sessions() as db:
                    assert not (await db.scalars(select(UserTool))).all(), "预览不得创建工具"
                body, published = await apply(
                    first,
                    "first",
                    [
                        {
                            "remote_name": "search",
                        },
                        {
                            "remote_name": "unknown",
                            "effect": "read",
                        },
                    ],
                )
                assert [row["status"] for row in published["items"]] == ["published", "failed"]
                tool_id = published["items"][0]["tool_id"]
                count = len(discoveries)
                repeat = await client.post(f"/api/v1/mcp-connections/{cid}/sync", json=body)
                assert repeat.json()["data"] == published and len(discoveries) == count, "重试不重复发现或发布"
                conflict = await client.post(
                    f"/api/v1/mcp-connections/{cid}/sync",
                    json={
                        **body,
                        "selections": [
                            {
                                "remote_name": "unknown",
                            }
                        ],
                    },
                )
                assert conflict.status_code == 409
                async with sessions.begin() as db:
                    tool = await db.get(UserTool, tool_id)
                    original_name = model_tool_name(tool)
                    db.add(AgentConfig(id=10, owner_id=1, name="搜索助手", default_model_id=1))
                    db.add(AgentTool(agent_id=10, tool_id=tool_id, tool_version=1))
                    old_run = await create_run(db, 1, input_text="旧请求", agent_id=10)
                    await snapshot_run_tools(db, old_run.id, 10, 1)
                    old_run_id = old_run.id
                unchanged = await preview()
                assert unchanged["items"][0]["state"] == "unchanged"
                definitions[0]["input_schema"]["required"] = ["title", "project_id"]
                definitions[0]["input_schema"]["properties"]["project_id"] = {
                    "type": "string",
                }
                stale = await client.post(
                    f"/api/v1/mcp-connections/{cid}/sync",
                    json={
                        **body,
                        "request_id": "stale",
                    },
                )
                assert stale.status_code == 409
                changed = await preview()
                assert changed["items"][0]["changes"] == ["schema"]
                await apply(
                    changed,
                    "second",
                    [
                        {
                            "remote_name": "search",
                            "tool_id": tool_id,
                            "expected_version": 1,
                        }
                    ],
                )
                async with sessions() as db:
                    assert (await db.scalar(select(AgentTool).where(AgentTool.agent_id == 10))).tool_version == 1
                    assert (await db.scalar(select(RunTool).where(RunTool.run_id == old_run_id))).tool_version == 1
                edited_agent = await client.put(
                    "/api/v1/agents/10",
                    json={
                        "expected_version": 1,
                        "name": "搜索助手改名",
                        "tool_ids": [tool_id],
                    },
                )
                assert edited_agent.status_code == 200, edited_agent.text
                async with sessions() as db:
                    assert (await db.scalar(select(AgentTool).where(AgentTool.agent_id == 10))).tool_version == 1, "普通编辑不能静默升级已选工具"
                upgrades = (await client.get("/api/v1/agents/10/tool-upgrades")).json()["data"]
                assert upgrades["items"][0]["current_version"] == 1 and upgrades["items"][0]["target_version"] == 2
                upgrade = {
                    "request_id": "upgrade-1",
                    "expected_version": upgrades["agent_version"],
                    "preview_hash": upgrades["preview_hash"],
                    "selections": [
                        {
                            "tool_id": tool_id,
                            "expected_version": 1,
                            "target_version": 2,
                        }
                    ],
                }
                upgraded = await client.post("/api/v1/agents/10/tool-upgrades", json=upgrade)
                assert upgraded.status_code == 200 and upgraded.json()["data"]["items"][0]["status"] == "upgraded", upgraded.text
                assert (await client.post("/api/v1/agents/10/tool-upgrades", json=upgrade)).json() == upgraded.json()
                async with sessions.begin() as db:
                    new_run = await create_run(db, 1, input_text="新请求", agent_id=10)
                    await snapshot_run_tools(db, new_run.id, 10, 1)
                    assert (await db.scalar(select(RunTool).where(RunTool.run_id == new_run.id))).tool_version == 2
                    assert (await db.scalar(select(RunTool).where(RunTool.run_id == old_run_id))).tool_version == 1
                edited = await client.put(
                    f"/api/v1/mcp-connections/{cid}",
                    json={
                        "expected_version": 1,
                        "name": "服务 A 改名",
                        "transport": "streamable_http",
                        "url": draft["url"],
                    },
                )
                assert edited.status_code == 200 and edited.json()["data"]["auth_type"] == "api_key", edited.text
                assert edited.json()["data"]["config_revoked_through"] == 0
                tenant = await client.put(
                    f"/api/v1/mcp-connections/{cid}",
                    json={
                        "expected_version": 2,
                        "name": "服务 A 改名",
                        "transport": "streamable_http",
                        "url": draft["url"],
                        "headers": {
                            "X-Tenant": "tenant-b",
                        },
                    },
                )
                assert tenant.status_code == 200 and tenant.json()["data"]["config_revoked_through"] == 2, tenant.text
                blocked = (await client.get(f"/api/v1/tools/{tool_id}")).json()["data"]
                assert blocked["callability"]["status"] == "blocked"
                rebind = await preview()
                assert "execution_config" in rebind["items"][0]["changes"]
                await apply(
                    rebind,
                    "third",
                    [
                        {
                            "remote_name": "search",
                            "tool_id": tool_id,
                            "expected_version": 2,
                        }
                    ],
                )
                async with sessions() as db:
                    assert model_tool_name(await db.get(UserTool, tool_id)) == original_name
                    assert (
                        await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool_id, UserToolVersion.version == 1))
                    ).input_schema == SCHEMA
                definitions[:] = [remote("renamed_search"), remote("unknown", read=None)]
                removed = await preview()
                assert removed["items"][0]["state"] == "removed" and removed["items"][-1]["state"] == "added"
                await apply(
                    removed,
                    "fourth",
                    [
                        {
                            "remote_name": "search",
                            "tool_id": tool_id,
                            "expected_version": 3,
                            "action": "disable",
                        }
                    ],
                )
                assert (await client.get(f"/api/v1/tools/{tool_id}")).json()["data"]["enabled"] is False
                # 两个连接提供同名远端函数，技术标识独立；展示名称变更不影响模型映射。
                other = await client.post(
                    "/api/v1/mcp-connections",
                    json={
                        **draft,
                        "name": "服务 B",
                    },
                )
                other_id = other.json()["data"]["id"]
                other_plan = (await client.post(f"/api/v1/mcp-connections/{other_id}/sync-preview")).json()["data"]
                other_body = {
                    "request_id": "same-function",
                    "expected_version": 1,
                    "preview_hash": other_plan["preview_hash"],
                    "selections": [
                        {
                            "remote_name": "unknown",
                            "effect": "read",
                            "read_only_reason": "已核对只执行查询",
                        }
                    ],
                }
                other_created, concurrent_retry = await asyncio.gather(
                    client.post(f"/api/v1/mcp-connections/{other_id}/sync", json=other_body),
                    client.post(f"/api/v1/mcp-connections/{other_id}/sync", json=other_body),
                )
                assert other_created.status_code == concurrent_retry.status_code == 200, (other_created.text, concurrent_retry.text)
                assert other_created.json() == concurrent_retry.json(), "并发重复提交须返回相同回执"

                other_tool = other_created.json()["data"]["items"][0]["tool_id"]
                assert other_created.json()["data"]["items"][0]["status"] == "published", other_created.text
                unknown_plan = await preview()
                _, unknown_result = await apply(
                    unknown_plan,
                    "confirm-unknown",
                    [
                        {
                            "remote_name": "unknown",
                            "effect": "read",
                            "read_only_reason": "已核对只执行查询",
                        }
                    ],
                )
                unknown_id = unknown_result["items"][0]["tool_id"]
                async with sessions() as db:
                    assert model_tool_name(await db.get(UserTool, other_tool)) != model_tool_name(await db.get(UserTool, unknown_id))
                definitions[1]["input_schema"]["properties"]["new_option"] = {
                    "type": "string",
                }
                reconfirm = await preview()
                reconfirm_row = next(row for row in reconfirm["items"] if row["tool_id"] == unknown_id)
                assert reconfirm_row["requires_read_confirmation"]
                _, refused = await apply(
                    reconfirm,
                    "needs-confirmation",
                    [
                        {
                            "remote_name": "unknown",
                            "tool_id": unknown_id,
                            "expected_version": 1,
                        }
                    ],
                )
                assert refused["items"][0]["status"] == "failed"
                await apply(
                    await preview(),
                    "confirmed-again",
                    [{"remote_name": "unknown", "tool_id": unknown_id, "expected_version": 1, "read_only_reason": "参数变化后再次核对只读"}],
                )
                actor.id = 2
                assert (await client.post(f"/api/v1/mcp-connections/{cid}/sync-preview")).status_code == 404
                assert (await client.get("/api/v1/agents/10/tool-upgrades")).status_code == 404
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(exercise())
