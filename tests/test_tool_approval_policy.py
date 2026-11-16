"""验证用户工具可选择审批，并在版本更新时保留设置。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.base import Base
from app.models.tool import UserToolVersion


@pytest.mark.parametrize("kind,read_only", [("script", None), ("mcp", True), ("mcp", False)])
def test_tool_approval_defaults_and_version_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, read_only: bool | None) -> None:
    """新增默认免审批，显式启用、保留及关闭均生成可追溯的新版本。"""

    async def discover(url: str, token: str | None) -> list[dict]:
        """模拟远端声明，测试期间不访问外部服务。"""
        return [
            {
                "name": "lookup",
                "input_schema": {
                    "type": "object",
                },
                "read_only": read_only,
            },
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "1.1.1.1")

    async def exercise() -> None:
        """通过真实 HTTP 接口创建及更新工具，并检查数据库中的历史版本。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'approval-policy.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="approval-owner", password="unused", role="user", status=1, is_deleted=0)

        async def session_override():
            """为请求提供隔离数据库。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db_session] = session_override
        app.dependency_overrides[get_current_admin] = lambda: actor
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(actor)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                draft = {
                    "name": "approval_tool",
                    "description": "测试审批配置",
                    "kind": kind,
                }
                if kind == "mcp":
                    draft.update(
                        {
                            "mcp_url": "https://mcp.example.com/mcp",
                            "mcp_tool_name": "lookup",
                        },
                    )
                else:
                    draft.update(
                        {
                            "script_language": "python",
                            "source_code": "print('{}')",
                            "input_schema": {
                                "type": "object",
                            },
                        },
                    )
                response = await client.post("/api/v1/tools", json=draft)
                assert response.status_code == 201, response.text
                original = response.json()["data"]
                assert original["requires_approval"] is False
                if kind == "mcp":
                    assert original["effect"] == ("read" if read_only else "write")
                tool_id = original["id"]
                for version, changes, expected in [
                    (
                        1,
                        {
                            "requires_approval": True,
                        },
                        True,
                    ),
                    (
                        2,
                        {
                            "description": "仅修改用途",
                        },
                        True,
                    ),
                    (
                        3,
                        {
                            "requires_approval": False,
                        },
                        False,
                    ),
                    (
                        4,
                        {
                            "description": "保持免审批",
                        },
                        False,
                    ),
                ]:
                    response = await client.put(
                        f"/api/v1/tools/{tool_id}",
                        json={
                            "expected_version": version,
                            **changes,
                        },
                    )
                    assert response.status_code == 200, response.text
                    assert response.json()["data"]["requires_approval"] is expected
                explicit = await client.post(
                    "/api/v1/tools",
                    json={
                        **draft,
                        "name": "explicit_approval",
                        "requires_approval": True,
                    },
                )
                assert explicit.status_code == 201, explicit.text
                assert explicit.json()["data"]["requires_approval"] is True
                for invalid in [None, "false", 0]:
                    response = await client.put(
                        f"/api/v1/tools/{tool_id}",
                        json={
                            "expected_version": 5,
                            "requires_approval": invalid,
                        },
                    )
                    assert response.status_code in (400, 422), response.text
                stale = await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 1,
                        "requires_approval": True,
                    },
                )
                assert stale.status_code == 409
                async with sessions() as db:
                    versions = (await db.scalars(select(UserToolVersion).where(UserToolVersion.tool_id == tool_id).order_by(UserToolVersion.version))).all()
                    assert [item.requires_approval for item in versions] == [False, True, True, False, False]
                    assert versions[0].content_hash != versions[1].content_hash
                    assert versions[1].content_hash == versions[2].content_hash
                    assert versions[1].content_hash != versions[3].content_hash
                    assert versions[3].content_hash == versions[4].content_hash
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_current_admin, None)
            await engine.dispose()

    asyncio.run(exercise())


def test_disabling_approval_applies_to_existing_agent_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保存关闭审批后，无需重新编辑 Agent，下一轮实际工具策略即免审批。"""
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    from app.models.agent_config import AgentConfig
    from app.models.agent_run import AgentRun
    from app.models.run_approval import RunApproval
    from app.models.tool import AgentTool, RunTool, ToolInvocation, UserTool
    from app.services.run_executor import execute_text_run
    from app.services.run_manager import create_run
    from app.services.user_tools import snapshot_run_tools
    from app.services.worker_tools import make_worker_tool_resolver

    class LookupModel(FakeMessagesListChatModel):
        """提供固定工具调用，复用真实图而不访问模型服务。"""

        def bind_tools(self, tools, *, tool_choice=None, **kwargs):
            """允许图注册正式 worker 工具。"""
            return self

    schema = {
        "type": "object",
    }
    invoked = []

    async def invoke(url, name, arguments, token, timeout, expected_schema, **kwargs):
        """核对开关不会升级原 Agent 固定的连接或执行定义。"""
        assert url == "https://mcp.example.com/mcp"
        assert name == "lookup" and arguments == {} and expected_schema == schema
        invoked.append(name)
        return '{"ok": true}'

    async def discover(url: str, token: str | None) -> list[dict]:
        """返回稳定的只读 MCP 定义。"""
        return [
            {
                "name": "lookup",
                "input_schema": schema,
                "read_only": True,
            },
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "1.1.1.1")
    monkeypatch.setattr("app.services.worker_tools.invoke_mcp_tool", invoke)

    async def exercise() -> None:
        """复现页面保存工具后直接返回原 Agent 继续对话的调用链。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agent-approval-policy.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="policy-owner", password="unused", role="user", status=1, is_deleted=0)

        async def session_override():
            """使用隔离数据库处理请求。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db_session] = session_override
        app.dependency_overrides[get_current_admin] = lambda: actor
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        actor,
                        AgentConfig(id=10, owner_id=1, name="existing agent", default_model_id=1, status=1, is_deleted=0),
                        UserTool(
                            id=3,
                            owner_id=1,
                            name="lookup",
                            description="查询公开资料",
                            kind="mcp",
                            visibility="private",
                            enabled=True,
                            current_version=1,
                            status=1,
                            is_deleted=0,
                        ),
                        UserToolVersion(
                            tool_id=3,
                            version=1,
                            content_hash="a" * 64,
                            input_schema=schema,
                            effect="read",
                            recovery_policy="safe",
                            requires_approval=True,
                            verified=True,
                            mcp_url="https://mcp.example.com/mcp",
                            mcp_tool_name="lookup",
                            share_credentials=False,
                            status=1,
                            is_deleted=0,
                        ),
                        AgentTool(agent_id=10, tool_id=3, tool_version=1, status=1, is_deleted=0),
                    ],
                )

            async def execute(run_id: str):
                """执行真实 Agent 图，断言实际是否进入审批。"""
                model = LookupModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "user_tool_3_lookup",
                                    "args": {},
                                    "id": "call-lookup",
                                },
                            ],
                        ),
                        AIMessage(content="查询完成"),
                    ],
                )
                return await execute_text_run(
                    sessions,
                    run_id,
                    model,
                    tmp_path / "checkpoints.db",
                    resolve_tools=make_worker_tool_resolver(sessions),
                    lease_seconds=10,
                    poll_seconds=1,
                )

            async with sessions.begin() as db:
                old_run = await create_run(db, 1, agent_id=10, input_text="旧一轮查询")
                await snapshot_run_tools(db, old_run.id, 10, 1)
            waiting = await execute(old_run.id)
            assert waiting.state == "waiting_for_approval"
            assert invoked == []
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.put(
                    "/api/v1/tools/3",
                    json={
                        "expected_version": 1,
                        "requires_approval": False,
                    },
                )
                assert response.status_code == 200, response.text
                assert response.json()["data"]["requires_approval"] is False
            async with sessions.begin() as db:
                run = await create_run(db, 1, agent_id=10, input_text="继续查询")
                await snapshot_run_tools(db, run.id, 10, 1)
            async with sessions.begin() as db:
                queued = await db.get(AgentRun, run.id)
                tools, approvals = await make_worker_tool_resolver(sessions)(db, queued)
                assert "user_tool_3_lookup" in {tool.name for tool in tools}
                assert approvals.get("user_tool_3_lookup", False) is False, "关闭审批后原 Agent 的下一轮仍使用旧审批设置"
                fixed = await db.scalar(select(RunTool).where(RunTool.run_id == run.id))
                assert fixed.tool_version == 1 and fixed.content_hash == "a" * 64
                assert fixed.requires_approval is False
                binding = await db.scalar(select(AgentTool).where(AgentTool.agent_id == 10))
                assert binding.tool_version == 1
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.put(
                    "/api/v1/tools/3",
                    json={
                        "expected_version": 2,
                        "requires_approval": True,
                        "mcp_url": "https://new-mcp.example.com/mcp",
                    },
                )
                assert response.status_code == 200, response.text
            result = await execute(run.id)
            assert result.state == "completed", result.failure_code
            assert invoked == ["lookup"]
            async with sessions.begin() as db:
                assert (await db.get(AgentRun, old_run.id)).state == "waiting_for_approval"
                old_approval = await db.scalar(select(RunApproval).where(RunApproval.run_id == old_run.id))
                assert old_approval.state == "pending" and old_approval.decided_at is None
                assert await db.scalar(select(RunApproval.id).where(RunApproval.run_id == run.id)) is None
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
                assert invocation.state == "completed" and invocation.tool_version == 1
                next_run = await create_run(db, 1, agent_id=10, input_text="重新开启审批后的查询")
                await snapshot_run_tools(db, next_run.id, 10, 1)
            next_result = await execute(next_run.id)
            assert next_result.state == "waiting_for_approval"
            assert invoked == ["lookup"]
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_current_admin, None)
            await engine.dispose()

    asyncio.run(exercise())


def test_run_approval_snapshot_migration_preserves_legacy_runs(tmp_path: Path) -> None:
    """真实迁移往返保留旧运行版本，旧审批快照为空时继续沿用历史设置。"""
    import os
    import sqlite3
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "approval-migration.db"
    environment = os.environ.copy()
    environment.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        },
    )

    def migrate(*arguments: str) -> None:
        """只对隔离数据库执行迁移或结构一致性检查。"""
        completed = subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert completed.returncode == 0, completed.stderr

    migrate("upgrade", "055")
    with sqlite3.connect(database) as db:
        db.execute(
            "INSERT INTO yp_run_tool (run_id,tool_id,tool_version,content_hash,create_time,update_time,create_timestamp,update_timestamp) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("legacy-run", 3, 1, "a" * 64, "2026-11-16 00:00:00", "2026-11-16 00:00:00", 1794758400, 1794758400),
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT tool_version, requires_approval FROM yp_run_tool").fetchall() == [(1, None)]
        db.execute("UPDATE yp_run_tool SET requires_approval = 0")
    migrate("check")
    migrate("downgrade", "055")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT tool_version, content_hash FROM yp_run_tool").fetchall() == [(1, "a" * 64)]
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT tool_version, requires_approval FROM yp_run_tool").fetchall() == [(1, None)]
