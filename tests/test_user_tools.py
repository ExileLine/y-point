# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : test_user_tools.py

"""验证用户 Tool 的权限、版本快照及不依赖 Skill 的对话调用。"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
import socket

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.password import hash_password
from app.core.middleware import _mask_fields
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.skill import AgentSkill, Skill, SkillVersion
from app.models.tool import AgentTool, RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services.run_executor import execute_text_run
from app.services.run_manager import claim_run, create_run, fail_run
from app.services import mcp_tools
from app.services.mcp_tools import _PinnedPublicTransport
from app.services.script_sandbox import SandboxConfigurationError, run_script
from app.services.skills import snapshot_run_skills, validate_package
from app.services.user_tools import (
    model_tool_name,
    skill_tool_catalog,
    snapshot_run_tools,
    tool_name_aliases,
    validate_mcp_url,
    validate_tool_definition,
)
from app.services.worker_tools import make_worker_tool_resolver


class ToolModel(FakeMessagesListChatModel):
    """允许真实 DeepAgents 图绑定脚本 Tool 的假模型。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """使用预先设置的调用序列。"""
        return self


def _schema() -> dict:
    """生成仅接受一个整数的工具输入结构。"""
    return {
        "type": "object",
        "properties": {
            "value": {
                "type": "integer",
            },
        },
        "required": ["value"],
        "additionalProperties": False,
    }


def test_tool_schema_error_distinguishes_output_example_from_size_limit() -> None:
    """将返回结果误填为输入结构时，提示类型错误而非误报容量超限。"""
    fields = {
        "input_schema": {
            "iso8601": "2026-11-02T14:30:00.123456+08:00",
            "timestamp": 1793601000,
            "weekday_cn": "星期一",
        },
    }
    with pytest.raises(ValueError, match="不能提交示例参数或返回结果"):
        validate_tool_definition("script", "current_time", fields)
    fields["input_schema"] = {
        "type": "object",
        "properties": {
            "large": {
                "type": "string",
                "description": "x" * (16 * 1024),
            },
        },
    }
    with pytest.raises(ValueError, match="不能超过 16 KiB"):
        validate_tool_definition("script", "current_time", fields)


def test_tool_request_logging_masks_credentials_and_source() -> None:
    """HTTP 日志不得暴露 MCP Bearer 凭据或用户脚本源码。"""
    assert _mask_fields(
        {
            "mcp_bearer_token": "secret-token",
            "source_code": "print('secret')",
        },
        set(),
    ) == {
        "mcp_bearer_token": "***",
        "source_code": "***",
    }


def test_private_public_tool_and_agent_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """跨用户只能选公开 Tool，且创建 Agent 可不关联 Skill。"""

    async def exercise() -> None:
        """在隔离库中调用 Tool 与 Agent 的公开接口。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tool-api.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="tool-a", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                        Admin(id=2, username="tool-b", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                        ProviderConnection(
                            id=3,
                            name="test-provider",
                            name_key="test-provider",
                            kind="ollama",
                            base_url="http://localhost:11434/v1",
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=5,
                            provider_id=3,
                            external_id="test-tool-model",
                            display_name="Tool Model",
                            capabilities=["text", "tools"],
                            verified_capabilities=["text", "tools"],
                            capabilities_verified=True,
                            context_window=8192,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                    ]
                )

            async def session_override():
                """让 HTTP 请求使用隔离业务库。"""
                async with sessions() as db:
                    yield db

            app.dependency_overrides[get_db_session] = session_override
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

                    async def auth(username: str) -> dict[str, str]:
                        """登录测试用户并取回 Bearer 头。"""
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

                    owner, other = await auth("tool-a"), await auth("tool-b")
                    payload = {
                        "name": "double_value",
                        "description": "将输入整数乘以二",
                        "kind": "script",
                        "input_schema": _schema(),
                        "script_language": "python",
                        "source_code": "import json, sys\nargs = json.load(sys.stdin)\nprint(json.dumps({'result': args['value'] * 2}))\n",
                    }
                    created = await client.post("/api/v1/tools", headers=owner, json=payload)
                    assert created.status_code == 201, created.text
                    tool_id = created.json()["data"]["id"]
                    assert created.json()["data"]["verified"] is False
                    assert created.json()["data"]["enabled"] is False
                    assert (
                        await client.post(
                            "/api/v1/tools",
                            headers=owner,
                            json={
                                **payload,
                                "visibility": "public",
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.post(
                            "/api/v1/agents",
                            headers=owner,
                            json={
                                "name": "Unverified Tool",
                                "default_model_id": 5,
                                "tool_ids": [tool_id],
                            },
                        )
                    ).status_code == 404
                    assert (await client.get(f"/api/v1/tools/{tool_id}", headers=other)).status_code == 404
                    catalog = (await client.get("/api/v1/tools/catalog", headers=other)).json()["data"]
                    assert catalog["builtin"] and catalog["user_tools"] == []

                    async def missing_dependency(*args, **kwargs):
                        """模拟镜像未预装依赖，不公开脚本源码或错误输出。"""
                        raise SandboxConfigurationError("沙箱镜像缺少依赖 example_lib，请联系维护者构建预装依赖的镜像")

                    with monkeypatch.context() as patch:
                        patch.setattr("app.api.v1.tools.run_script", missing_dependency)
                        failed = await client.post(
                            f"/api/v1/tools/{tool_id}/test",
                            headers=owner,
                            json={
                                "arguments": {
                                    "value": 21,
                                },
                            },
                        )
                        assert failed.status_code == 400
                        assert "沙箱镜像缺少依赖 example_lib" in failed.text
                        unchanged = (await client.get(f"/api/v1/tools/{tool_id}", headers=owner)).json()["data"]
                        assert unchanged["verified"] is False and unchanged["enabled"] is False
                    tested = await client.post(
                        f"/api/v1/tools/{tool_id}/test",
                        headers=owner,
                        json={
                            "arguments": {
                                "value": 21,
                            },
                        },
                    )
                    assert tested.status_code == 200, tested.text
                    published = await client.put(
                        f"/api/v1/tools/{tool_id}",
                        headers=owner,
                        json={
                            "expected_version": 1,
                            "visibility": "public",
                        },
                    )
                    assert published.status_code == 200, published.text
                    detail = (await client.get(f"/api/v1/tools/{tool_id}", headers=other)).json()["data"]
                    assert "source_code" not in detail and "mcp_url" not in detail
                    assert detail["model_name"] == f"user_tool_{tool_id}_double_value"
                    assert (
                        await client.put(
                            f"/api/v1/tools/{tool_id}",
                            headers=other,
                            json={
                                "expected_version": 2,
                            },
                        )
                    ).status_code == 404
                    agent = await client.post(
                        "/api/v1/agents",
                        headers=other,
                        json={
                            "name": "Only Tool",
                            "default_model_id": 5,
                            "skill_ids": [],
                            "tool_ids": [tool_id],
                        },
                    )
                    assert agent.status_code == 201, agent.text
                    assert agent.json()["data"]["tool_ids"] == [tool_id]
                    assert agent.json()["data"]["skill_ids"] == []
                    async with sessions.begin() as db:
                        audit_run = await create_run(db, 2, input_text="audit")
                        db.add(
                            ToolInvocation(
                                run_id=audit_run.id,
                                call_id="call-audit",
                                tool_id=tool_id,
                                tool_version=2,
                                state="completed",
                                arguments_hash="a" * 64,
                                is_deleted=0,
                                status=1,
                            )
                        )
                    calls_response = await client.get(f"/api/v1/tools/runs/{audit_run.id}/calls", headers=other)
                    assert calls_response.status_code == 200
                    assert calls_response.json()["data"][0]["state"] == "completed"
                    assert isinstance(calls_response.json()["data"][0]["create_time"], str)
                    assert (await client.get(f"/api/v1/tools/runs/{audit_run.id}/calls", headers=owner)).status_code == 404
                    changed = await client.put(
                        f"/api/v1/tools/{tool_id}",
                        headers=owner,
                        json={
                            "expected_version": 2,
                            "source_code": "import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'result': a['value'] * 3}))\n",
                        },
                    )
                    assert changed.status_code == 200, changed.text
                    assert changed.json()["data"]["verified"] is False
                    assert changed.json()["data"]["enabled"] is False
                    assert changed.json()["data"]["visibility"] == "private"
                    async with sessions() as db:
                        selected = await db.scalar(select(UserTool).where(UserTool.id == tool_id))
                        assert selected.visibility == "private"
            finally:
                app.dependency_overrides.pop(get_db_session, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_script_sandbox_blocks_network_and_times_out() -> None:
    """真实容器不得连网，超时脚本必须终止。"""

    async def exercise() -> None:
        """分别检查网络隔离和运行时限。"""
        with pytest.raises(ValueError, match="输入超过"):
            await run_script(
                "python",
                "print('{}')",
                {
                    "data": "x" * (64 * 1024),
                },
                10,
            )
        result = await run_script("python", "import httpx, json\nprint(json.dumps({'httpx': httpx.__version__}))", {}, 10)
        assert json.loads(result)["httpx"] == "0.28.1"
        with pytest.raises(ValueError, match="沙箱镜像缺少依赖 yp_missing_dependency"):
            await run_script("python", "import yp_missing_dependency", {}, 10)
        with pytest.raises(ValueError, match="脚本执行失败"):
            await run_script("python", "import httpx\nhttpx.get('https://example.com', timeout=1)\n", {}, 10)
        with pytest.raises(ValueError, match="脚本执行失败"):
            await run_script("python", "import socket\nsocket.create_connection(('1.1.1.1', 80), timeout=1)\n", {}, 10)
        with pytest.raises(TimeoutError):
            await run_script("python", "while True: pass\n", {}, 1)

    asyncio.run(exercise())


def test_mcp_address_is_pinned_to_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP 拒绝私网解析结果，并在传输层固定已验证公网 IP。"""

    def private_address(*args, **kwargs):
        """模拟域名解析到内网。"""
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", private_address)
    with pytest.raises(ValueError, match="内网"):
        validate_mcp_url("https://mcp.example.com/mcp")

    async def exercise() -> None:
        """截获固定 IP 的请求，不访问外部服务。"""
        seen = []

        def respond(request: httpx.Request) -> httpx.Response:
            """记录最终连接目标和 TLS 域名。"""
            seen.append(request)
            return httpx.Response(200, text="ok")

        transport = _PinnedPublicTransport("mcp.example.com", "1.1.1.1")
        await transport.transport.aclose()
        transport.transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://mcp.example.com/mcp")
            assert response.status_code == 200
            with pytest.raises(ValueError, match="目标已变化"):
                await client.get("https://other.example.com/mcp")
        assert seen[0].url.host == "1.1.1.1"
        assert seen[0].headers["Host"] == "mcp.example.com"
        assert seen[0].extensions["sni_hostname"] == "mcp.example.com"

    asyncio.run(exercise())


def test_terminal_run_marks_unfinished_tool_call_unknown(tmp_path: Path) -> None:
    """执行器失败时未完成的 Tool 调用保留未知状态，避免误判为成功。"""

    async def exercise() -> None:
        """在隔离库创建运行、调用审计，再将运行置为失败。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tool-audit.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="check")
            async with sessions.begin() as db:
                claimed = await claim_run(db, run.id)
                db.add(
                    ToolInvocation(
                        run_id=run.id,
                        call_id="unfinished",
                        tool_id=3,
                        tool_version=1,
                        state="running",
                        arguments_hash="a" * 64,
                        is_deleted=0,
                        status=1,
                    )
                )
            async with sessions.begin() as db:
                await fail_run(db, run.id, claimed.lease_token, "runtime_error")
            async with sessions() as db:
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.call_id == "unfinished"))
                assert invocation.state == "unknown"
                assert invocation.failure_code == "run_terminated"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_public_mcp_without_credentials_can_be_invoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """公开且无凭据的 MCP Tool 可供其他用户直接关联和调用。"""

    async def fake_invoke(url, name, arguments, token, timeout, schema, **kwargs):
        """代替外部 MCP 服务，保留运行时授权检查。"""
        assert timeout == 120
        assert token is None and arguments == {
            "repoName": "modelcontextprotocol/python-sdk",
        }
        return '{"ok": true}'

    monkeypatch.setattr("app.services.worker_tools.invoke_mcp_tool", fake_invoke)

    async def exercise() -> None:
        """在隔离业务库中领取 Run 并执行真实注册的用户 Tool。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'public-mcp.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="mcp-owner", password="unused", role="user", status=1, is_deleted=0),
                        Admin(id=2, username="mcp-user", password="unused", role="user", status=1, is_deleted=0),
                        UserTool(
                            id=3,
                            owner_id=1,
                            name="wiki_structure",
                            description="读取公开文档结构",
                            kind="mcp",
                            visibility="public",
                            enabled=True,
                            current_version=1,
                            status=1,
                            is_deleted=0,
                        ),
                        UserToolVersion(
                            tool_id=3,
                            version=1,
                            content_hash="a" * 64,
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "repoName": {
                                        "type": "string",
                                    },
                                },
                                "required": ["repoName"],
                            },
                            effect="read",
                            requires_approval=True,
                            verified=True,
                            mcp_url="https://mcp.deepwiki.com/mcp",
                            mcp_tool_name="read_wiki_structure",
                            share_credentials=False,
                            status=1,
                            is_deleted=0,
                        ),
                    ]
                )
                await db.flush()
                run = await create_run(db, 2, input_text="read wiki")
                db.add(
                    RunTool(
                        run_id=run.id,
                        tool_id=3,
                        tool_version=1,
                        content_hash="a" * 64,
                        status=1,
                        is_deleted=0,
                    )
                )
            async with sessions.begin() as db:
                claimed = await claim_run(db, run.id)
                tools, approvals = await make_worker_tool_resolver(sessions)(db, claimed)
                assert approvals["user_tool_3_wiki_structure"] is True
            tool = next(item for item in tools if item.name == "user_tool_3_wiki_structure")
            result = await tool.ainvoke(
                {
                    "repoName": "modelcontextprotocol/python-sdk",
                }
            )
            assert result == '{"ok": true}'
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("use_skill,short_name", [(False, False), (False, True), (True, True)])
def test_script_tool_runs_without_skill(tmp_path: Path, use_skill: bool, short_name: bool) -> None:
    """Agent 只关联 Tool 时，模型可在真实沙箱中调用并留下脱敏记录。"""

    async def exercise() -> None:
        """运行真实图、Docker 沙箱和隔离数据库。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tool-run.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="tool-owner", password="unused", role="user", status=1, is_deleted=0),
                        AgentConfig(
                            id=3,
                            owner_id=1,
                            name="Tool Agent",
                            default_model_id=1,
                            enabled=True,
                            version=1,
                            status=1,
                            is_deleted=0,
                        ),
                        UserTool(
                            id=4,
                            owner_id=1,
                            name="double_value",
                            description="将整数乘二",
                            kind="script",
                            visibility="private",
                            enabled=True,
                            current_version=1,
                            status=1,
                            is_deleted=0,
                        ),
                        UserToolVersion(
                            tool_id=4,
                            version=1,
                            content_hash="a" * 64,
                            input_schema=_schema(),
                            effect="read",
                            requires_approval=False,
                            verified=True,
                            script_language="python",
                            source_code="import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'result': a['value'] * 2}))\n",
                            share_credentials=False,
                            status=1,
                            is_deleted=0,
                        ),
                        AgentTool(agent_id=3, tool_id=4, tool_version=1, status=1, is_deleted=0),
                    ]
                )
                if use_skill:
                    files = {
                        "SKILL.md": "---\nname: double-number\ndescription: 将整数翻倍\n---\n调用 double_value.py 获取结果。",
                    }
                    name, description, manifest, digest = validate_package(files)
                    db.add(
                        Skill(
                            id=2,
                            owner_id=1,
                            name=name,
                            description=description,
                            category="数学",
                            visibility="private",
                            enabled=True,
                            current_version=1,
                            status=1,
                            is_deleted=0,
                        )
                    )
                    db.add(SkillVersion(skill_id=2, version=1, content_hash=digest, files=files, manifest=manifest, status=1, is_deleted=0))
                    db.add(AgentSkill(agent_id=3, skill_id=2, skill_version=1, status=1, is_deleted=0))
                await db.flush()
                run = await create_run(db, 1, agent_id=3, input_text="double 21")
                await snapshot_run_tools(db, run.id, 3, 1)
                if use_skill:
                    await snapshot_run_skills(db, run.id, 3, 1)
            model_name = model_tool_name(UserTool(id=4, name="double_value"))
            model = ToolModel(
                responses=[
                    *(
                        [
                            AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "read_file",
                                        "args": {
                                            "file_path": "/skills/double-number/SKILL.md",
                                        },
                                        "id": "call-read-skill",
                                    }
                                ],
                            )
                        ]
                        if use_skill
                        else []
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "double_value" if short_name else model_name,
                                "args": {
                                    "value": 21,
                                },
                                "id": "call-double",
                            }
                        ],
                    ),
                    AIMessage(content="42"),
                ]
            )
            result = await execute_text_run(
                sessions,
                run.id,
                model,
                tmp_path / "checkpoints.db",
                resolve_tools=make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=1,
            )
            assert result.state == "completed", result.failure_code
            async with sessions() as db:
                invocations = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id))).all()
                assert len(invocations) == 1
                assert invocations[0].state == "completed"
                assert len(invocations[0].arguments_hash) == 64
                assert (await db.scalars(select(RunTool).where(RunTool.run_id == run.id))).one().tool_version == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_tool_aliases_reject_ambiguity_and_reserved_names() -> None:
    """同名、运行时保留名和其他工具规范名都不得被别名覆盖。"""
    one = UserTool(id=6, owner_id=1, name="get_time")
    assert tool_name_aliases([one], frozenset()) == {
        "get_time": "user_tool_6_get_time",
    }
    other = UserTool(id=7, owner_id=2, name="get_time")
    assert tool_name_aliases([one, other], frozenset()) == {}
    assert tool_name_aliases([one], {"get_time"}) == {}
    colliding = UserTool(id=8, owner_id=1, name="user_tool_6_get_time")
    assert "user_tool_6_get_time" not in tool_name_aliases([one, colliding], frozenset())
    assert tool_name_aliases([], frozenset()) == {}
    catalog = skill_tool_catalog([one, other], {})
    assert "必须明确工具 ID/所有者" in catalog
    assert "user_tool_6_get_time" in catalog and "user_tool_7_get_time" in catalog


def test_mcp_modern_client_lifecycle_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """新 MCP 接口复用受限 HTTP 客户端，退出和异常时都关闭连接。"""
    seen = []
    monkeypatch.setattr(get_config(), "MCP_CONNECT_TIMEOUT_SECONDS", 4)

    @asynccontextmanager
    async def fake_stream(url, *, http_client):
        """模拟仅接受新版 SDK 参数的连接。"""
        assert url == "https://mcp.example.com/mcp"
        assert not http_client.is_closed
        assert http_client.headers["Authorization"] == "Bearer test-token"
        assert http_client.timeout.connect == 4 and http_client.timeout.read == 7
        assert not http_client.follow_redirects
        assert http_client._trust_env is False
        assert isinstance(http_client._transport, _PinnedPublicTransport)
        assert http_client._transport.address == "1.1.1.1"
        seen.append(http_client)
        yield "read", "write", None

    monkeypatch.setattr(mcp_tools, "validate_mcp_url", lambda url: "1.1.1.1")
    monkeypatch.setattr(mcp_tools, "streamable_http_client", fake_stream)

    async def exercise() -> None:
        """覆盖正常退出及执行期间异常的资源释放。"""
        async with mcp_tools._mcp_connection("https://mcp.example.com/mcp", "test-token", 7) as streams:
            assert streams[:2] == ("read", "write")
        assert seen[-1].is_closed
        with pytest.raises(RuntimeError):
            async with mcp_tools._mcp_connection("https://mcp.example.com/mcp", "test-token", 7):
                raise RuntimeError("session failed")
        assert seen[-1].is_closed

    asyncio.run(exercise())


def test_mcp_transport_rejects_same_origin_redirects() -> None:
    """即使新版 SDK 会处理同源跳转，固定 IP 传输层也必须先拒绝。"""

    async def exercise() -> None:
        """模拟同源 307 响应并确认只发出一次请求。"""
        seen = []

        def redirect(request: httpx.Request) -> httpx.Response:
            """返回同源重定向，不连接外部服务。"""
            seen.append(request)
            return httpx.Response(
                307,
                headers={
                    "Location": "https://mcp.example.com/other",
                },
            )

        transport = _PinnedPublicTransport("mcp.example.com", "1.1.1.1")
        await transport.transport.aclose()
        transport.transport = httpx.MockTransport(redirect)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with pytest.raises(ValueError, match="不允许重定向"):
                await client.post("https://mcp.example.com/mcp")
        assert len(seen) == 1

    asyncio.run(exercise())
