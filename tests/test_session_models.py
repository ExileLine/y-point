# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 17:32:23
# @Author  : yangyuexiong
# @File    : test_session_models.py

"""验证会话模型解耦、切换续聊、审批保护、幂等和历史线程隔离。"""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import ModelCapability, RunState, SessionModelReason
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.agent_session import AgentSession
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.tool import AgentTool
from app.services import session_models as session_models_module
from app.services.run_executor import execute_text_run
from app.services.session_models import effective_session_model, handoff_messages
from tests.test_agent_sessions import FailingSessionModel, SessionModel


@asynccontextmanager
async def model_environment(tmp_path: Path):
    """创建隔离业务库、两个供应商与两个用户，不调用外部模型。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'model-switch.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            for user_id in (1, 2):
                db.add(
                    Admin(
                        id=user_id,
                        username=f"switch-user-{user_id}",
                        password=hash_password("StrongPassword123"),
                        role="user",
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    ProviderConnection(
                        id=user_id,
                        name=f"Provider {user_id}",
                        name_key=f"provider-{user_id}",
                        kind="ollama",
                        base_url="http://localhost:11434/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    ModelCatalog(
                        id=user_id,
                        provider_id=user_id,
                        external_id=f"model-{user_id}",
                        display_name=f"Model {user_id}",
                        verified_capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                        capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                        context_window=32768,
                        input_price=Decimal("1"),
                        output_price=Decimal("2"),
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
            db.add(AgentConfig(id=1, owner_id=1, name="切换助手", default_model_id=1, system_prompt="帮助用户完成任务"))

        async def database():
            """为正式路由提供隔离数据库会话。"""
            async with sessions() as db:
                yield db

        previous = app.dependency_overrides.get(get_db_session)
        app.dependency_overrides[get_db_session] = database
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                headers = []
                for user_id in (1, 2):
                    response = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "identifier": f"switch-user-{user_id}",
                            "password": "StrongPassword123",
                        },
                    )
                    assert response.status_code == 200, response.text
                    headers.append(
                        {
                            "Authorization": f"Bearer {response.json()['data']['access_token']}",
                        },
                    )
                yield sessions, client, headers[0], headers[1]
        finally:
            if previous is None:
                app.dependency_overrides.pop(get_db_session, None)
            else:
                app.dependency_overrides[get_db_session] = previous
    finally:
        await engine.dispose()


async def new_session(client: httpx.AsyncClient, headers: dict) -> tuple[str, dict]:
    """创建会话并返回资源路径与版本快照。"""
    response = await client.post("/api/v1/agents/1/sessions", headers=headers, json={})
    assert response.status_code == 201, response.text
    session = response.json()["data"]
    return f"/api/v1/agents/1/sessions/{session['id']}", session


async def submit(client: httpx.AsyncClient, headers: dict, path: str, key: str, text: str, **kwargs) -> httpx.Response:
    """通过正式提交接口发送带幂等键的输入。"""
    return await client.post(
        f"{path}/runs",
        headers=headers,
        json={
            "request_key": key,
            "input_text": text,
            **kwargs,
        },
    )


def test_switch_preserves_completed_history_and_model_snapshots(tmp_path: Path) -> None:
    """跨供应商切换两次后仍续聊，且模型、历史记录、检查点各自独立。"""

    async def exercise() -> None:
        """用真实 DeepAgents 检查点和固定模型完成三轮切换。"""
        async with model_environment(tmp_path) as (sessions, client, alice, bob):
            path, session = await new_session(client, alice)
            assert session["model_id"] == 1
            response = await submit(client, alice, path, "one", "记住我的项目叫星河", model_id=1, expected_session_version=1)
            assert response.status_code == 201, response.text
            first = response.json()["data"]
            done = await execute_text_run(sessions, first["id"], SessionModel(responses=[AIMessage(content="已记住星河")]), tmp_path / "cp.db")
            assert done.state == RunState.COMPLETED
            session = (await client.get(path, headers=alice)).json()["data"]
            switched = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": session["version"],
                    "model_id": 2,
                },
            )
            assert switched.status_code == 200, switched.text
            next_session = switched.json()["data"]
            assert next_session["id"] == session["id"] and next_session["thread_id"] != session["thread_id"]
            assert next_session["model_id"] == 2
            assert (await client.get("/api/v1/agents/1", headers=alice)).json()["data"]["default_model_id"] == 1
            assert (await client.get(f"{path}/models", headers=bob)).status_code == 404
            assert (
                await client.put(
                    path,
                    headers=bob,
                    json={
                        "expected_version": next_session["version"],
                        "model_id": 1,
                    },
                )
            ).status_code == 404
            # 切换后的原提交重试仍返回原运行；同一键不能冒用另一模型。
            replay = await submit(client, alice, path, "one", "记住我的项目叫星河", model_id=1, expected_session_version=1)
            assert replay.status_code == 201 and replay.json()["data"]["id"] == first["id"]
            assert (await submit(client, alice, path, "one", "记住我的项目叫星河", model_id=2)).status_code == 409
            stale = await submit(client, alice, path, "stale", "过期页面", model_id=1)
            assert stale.status_code == 409 and stale.json()["data"]["reason"] == SessionModelReason.SELECTION
            stale_version = await submit(client, alice, path, "stale-version", "过期版本", expected_session_version=1)
            assert stale_version.status_code == 409 and stale_version.json()["data"]["reason"] == SessionModelReason.VERSION
            hijack = await client.post(
                "/api/v1/runs",
                headers=alice,
                json={
                    "model_id": 1,
                    "thread_id": session["thread_id"],
                    "input_text": "复用旧线程",
                },
            )
            assert hijack.status_code == 409
            SessionModel.seen_inputs.clear()
            SessionModel.seen_tool_results.clear()
            response = await submit(client, alice, path, "two", "项目叫什么？", model_id=2)
            assert response.status_code == 201, response.text
            second = response.json()["data"]
            done = await execute_text_run(sessions, second["id"], SessionModel(responses=[AIMessage(content="项目叫星河")]), tmp_path / "cp.db")
            assert done.state == RunState.COMPLETED
            assert "记住我的项目叫星河" in SessionModel.seen_inputs and "项目叫什么？" in SessionModel.seen_inputs
            assert not SessionModel.seen_tool_results
            history = (await client.get(f"{path}/messages", headers=alice)).json()["data"]
            assert len(history) == 4
            assert [item["model_id"] for item in history] == [1, 1, 2, 2]
            assert history[-1]["model_name"] == "Model 2" and history[-1]["provider_name"] == "Provider 2"
            session = (await client.get(path, headers=alice)).json()["data"]
            changed = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": session["version"],
                    "model_id": 1,
                },
            )
            assert changed.status_code == 200
            third = await submit(client, alice, path, "three", "继续星河项目")
            assert third.status_code == 201
            SessionModel.seen_inputs.clear()
            done = await execute_text_run(sessions, third.json()["data"]["id"], SessionModel(responses=[AIMessage(content="继续")]), tmp_path / "cp.db")
            assert done.state == RunState.COMPLETED
            assert {"记住我的项目叫星河", "项目叫什么？", "继续星河项目"} <= set(SessionModel.seen_inputs)
            async with sessions() as db:
                assert (await db.get(AgentRun, first["id"])).model_id == 1
                assert (await db.get(AgentRun, second["id"])).model_id == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("state", [RunState.QUEUED, RunState.RUNNING, RunState.WAITING_FOR_APPROVAL, RunState.CANCEL_REQUESTED])
def test_switch_rejects_all_active_states(tmp_path: Path, state: RunState) -> None:
    """排队、执行、待审批和取消中的 Run 都不能通过切换绕开。"""

    async def exercise() -> None:
        """验证拒绝后模型与执行线程均未变化。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, _session = await new_session(client, alice)
            run = (await submit(client, alice, path, "busy", "等待处理")).json()["data"]
            async with sessions.begin() as db:
                (await db.get(AgentRun, run["id"])).state = state
            session = (await client.get(path, headers=alice)).json()["data"]
            result = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": session["version"],
                    "model_id": 2,
                },
            )
            assert result.status_code == 409 and result.json()["data"]["reason"] == SessionModelReason.BUSY
            current = (await client.get(path, headers=alice)).json()["data"]
            assert current["model_id"] == 1 and current["thread_id"] == session["thread_id"]

    asyncio.run(exercise())


@pytest.mark.parametrize("issue", ["disabled", "provider", "text", "tools", "context", "pricing", "archived", "version"])
def test_switch_capability_budget_and_concurrency_guards(tmp_path: Path, issue: str) -> None:
    """候选目录和写入校验一致，拒绝不可用、能力、窗口、价格与版本冲突。"""

    async def exercise() -> None:
        """分别制造配置不兼容，检查失败没有旋转线程。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, session = await new_session(client, alice)
            async with sessions.begin() as db:
                model = await db.get(ModelCatalog, 2)
                agent = await db.get(AgentConfig, 1)
                if issue == "disabled":
                    model.status = 0
                elif issue == "provider":
                    (await db.get(ProviderConnection, 2)).status = 0
                elif issue == "text":
                    model.verified_capabilities = []
                elif issue == "tools":
                    model.verified_capabilities = [ModelCapability.TEXT]
                    db.add(AgentTool(agent_id=1, tool_id=123, tool_version=1))
                elif issue == "context":
                    model.context_window = 4096
                elif issue == "pricing":
                    agent.max_estimated_cost = Decimal("0.1")
                    model.input_price = None
                elif issue == "archived":
                    (await db.get(AgentSession, session["id"])).archived = True
            result = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": 9 if issue == "version" else 1,
                    "model_id": 2,
                },
            )
            assert result.status_code == 409, result.text
            assert result.json()["data"]["reason"] in set(SessionModelReason)
            current = (await client.get(path, headers=alice)).json()["data"]
            assert current["thread_id"] == session["thread_id"] and current["model_id"] == 1
            options = (await client.get(f"{path}/models", headers=alice)).json()["data"]
            alternate = next((option for option in options if option["id"] == 2), None)
            if issue == "text":
                assert alternate is None
            elif issue not in {"archived", "version"}:
                assert alternate is not None and not alternate["selectable"] and alternate["notice"]

    asyncio.run(exercise())


def test_failed_turn_is_not_imported_and_new_thread_can_retry(tmp_path: Path) -> None:
    """失败输入和检查点不污染跨模型上下文，切换后首次失败仍可续聊。"""

    async def exercise() -> None:
        """在成功历史后制造失败，再切换并验证可恢复。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, _session = await new_session(client, alice)
            one = (await submit(client, alice, path, "ok", "已完成的事实")).json()["data"]
            await execute_text_run(sessions, one["id"], SessionModel(responses=[AIMessage(content="有效回答")]), tmp_path / "cp.db")
            bad = (await submit(client, alice, path, "bad", "失败轮秘密")).json()["data"]
            failed = await execute_text_run(sessions, bad["id"], FailingSessionModel(responses=[AIMessage(content="unused")]), tmp_path / "cp.db")
            assert failed.state == RunState.FAILED
            session = (await client.get(path, headers=alice)).json()["data"]
            result = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": session["version"],
                    "model_id": 2,
                },
            )
            assert result.status_code == 200
            bad2 = (await submit(client, alice, path, "bad2", "新线程失败输入")).json()["data"]
            failed = await execute_text_run(sessions, bad2["id"], FailingSessionModel(responses=[AIMessage(content="unused")]), tmp_path / "cp.db")
            assert failed.state == RunState.FAILED
            retry = await submit(client, alice, path, "retry", "正确续聊")
            assert retry.status_code == 201, retry.text
            SessionModel.seen_inputs.clear()
            done = await execute_text_run(sessions, retry.json()["data"]["id"], SessionModel(responses=[AIMessage(content="继续成功")]), tmp_path / "cp.db")
            assert done.state == RunState.COMPLETED
            assert "已完成的事实" in SessionModel.seen_inputs
            assert "失败轮秘密" not in SessionModel.seen_inputs and "新线程失败输入" not in SessionModel.seen_inputs

    asyncio.run(exercise())


def test_legacy_model_fallback_and_bounded_handoff(tmp_path: Path, monkeypatch) -> None:
    """旧数据沿用成功模型；限制按完整轮次截取，附件和失败轮次不进入新上下文。"""

    async def exercise() -> None:
        """验证数据库历史重建顺序与容量提示。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, session = await new_session(client, alice)
            one = (await submit(client, alice, path, "fact", "已完成事实")).json()["data"]
            await execute_text_run(sessions, one["id"], SessionModel(responses=[AIMessage(content="已完成回答")]), tmp_path / "cp.db")
            async with sessions.begin() as db:
                saved = await db.get(AgentSession, session["id"])
                saved.model_id = None
                agent = await db.get(AgentConfig, 1)
                agent.default_model_id = 2
                assert await effective_session_model(db, saved, agent) == 1
            current = await submit(client, alice, path, "next", "本轮输入")
            assert current.json()["data"]["model_id"] == 1
            async with sessions() as db:
                run = await db.get(AgentRun, current.json()["data"]["id"])
                messages = await handoff_messages(db, run)
                assert [message.content for message in messages] == ["已完成事实", "已完成回答"]
                assert all(not getattr(message, "tool_calls", []) for message in messages)
                monkeypatch.setattr(session_models_module, "MAX_HANDOFF_CHARACTERS", 1)
                bounded = await handoff_messages(db, run)
                assert len(bounded) == 1 and "容量限制" in bounded[0].content

    asyncio.run(exercise())


def test_switch_does_not_replay_completed_tool_calls(tmp_path: Path) -> None:
    """已执行的工具只保留最终可见回答，换模型不会再次执行或注入原始工具结果。"""

    async def exercise() -> None:
        """使用真正的工具执行循环后切换供应商，核对副作用次数和目标模型输入。"""
        from langchain_core.tools import tool

        executions = []

        @tool
        def lookup_trip_policy() -> str:
            """读取测试差旅条款并记录一次调用。"""
            executions.append("called")
            return "工具内部原文：报销期限为十五个工作日"

        async def tools(db, run):
            """提供本轮唯一允许调用的测试工具。"""
            return [lookup_trip_policy], {}

        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, _session = await new_session(client, alice)
            one = (await submit(client, alice, path, "tool-fact", "查一下报销期限")).json()["data"]
            model = SessionModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "policy-call",
                                "name": "lookup_trip_policy",
                                "args": {},
                            },
                        ],
                    ),
                    AIMessage(content="十五个工作日内提交。"),
                ],
            )
            completed = await execute_text_run(sessions, one["id"], model, tmp_path / "cp.db", resolve_tools=tools)
            assert completed.state == RunState.COMPLETED and executions == ["called"]
            session = (await client.get(path, headers=alice)).json()["data"]
            result = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": session["version"],
                    "model_id": 2,
                },
            )
            assert result.status_code == 200, result.text
            two = (await submit(client, alice, path, "tool-next", "根据刚才结论继续")).json()["data"]
            SessionModel.seen_tool_results.clear()
            completed = await execute_text_run(
                sessions,
                two["id"],
                SessionModel(responses=[AIMessage(content="继续整理")]),
                tmp_path / "cp.db",
                resolve_tools=tools,
            )
            assert completed.state == RunState.COMPLETED
            assert executions == ["called"] and not SessionModel.seen_tool_results

    asyncio.run(exercise())


def test_session_model_migration_backfills_existing_records(tmp_path: Path) -> None:
    """迁移优先回填成功模型，未成功会话沿用最近运行，空会话继承默认模型。"""
    import os
    import subprocess
    import sys
    from datetime import timedelta

    from app.models.base import shanghai_now

    database = tmp_path / "backfill.db"
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        },
    )
    root = Path(__file__).resolve().parents[1]

    def migrate(*args: str) -> None:
        """在独立 SQLite 库执行正式 Alembic 命令。"""
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True, text=True)

    async def seed() -> None:
        """插入已完成、后续失败、仅失败和没有运行的会话。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions.begin() as db:
                db.add(AgentConfig(id=1, owner_id=1, name="迁移助手", default_model_id=3, system_prompt=""))
                for name in ("completed", "failed", "empty"):
                    db.add(AgentSession(id=name, owner_id=1, agent_id=1, title=name, thread_id=f"thread-{name}", model_id=9))
                now = shanghai_now(naive=True)
                for run_id, session_id, model_id, state, offset in (
                    ("success", "completed", 1, RunState.COMPLETED, 0),
                    ("later-failure", "completed", 2, RunState.FAILED, 1),
                    ("only-failure", "failed", 2, RunState.FAILED, 2),
                ):
                    db.add(
                        AgentRun(
                            id=run_id,
                            owner_id=1,
                            agent_id=1,
                            session_id=session_id,
                            model_id=model_id,
                            thread_id=f"thread-{session_id}",
                            state=state,
                            create_time=now + timedelta(seconds=offset),
                        ),
                    )
        finally:
            await engine.dispose()

    async def verify() -> None:
        """读取迁移回填值，确认没有错误跟随 Agent 当前默认模型。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions() as db:
                for name, model_id in (("completed", 1), ("failed", 2), ("empty", 3)):
                    assert (await db.get(AgentSession, name)).model_id == model_id
        finally:
            await engine.dispose()

    migrate("upgrade", "head")
    asyncio.run(seed())
    migrate("downgrade", "042")
    migrate("upgrade", "head")
    asyncio.run(verify())
    migrate("check")


def test_switch_and_submit_compete_on_same_session_version(tmp_path: Path) -> None:
    """并发切换和发送只允许一个请求提交，避免旧线程运行被切换绕开。"""

    async def exercise() -> None:
        """使用同一读取版本并发提交，再核对模型快照与活动运行所属线程。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, original = await new_session(client, alice)
            switched, submitted = await asyncio.gather(
                client.put(
                    path,
                    headers=alice,
                    json={
                        "expected_version": 1,
                        "model_id": 2,
                    },
                ),
                submit(client, alice, path, "race", "并发发送", model_id=1, expected_session_version=1),
            )
            assert sorted([switched.status_code, submitted.status_code]) in ([200, 409], [201, 409])
            current = (await client.get(path, headers=alice)).json()["data"]
            if submitted.status_code == 201:
                run = submitted.json()["data"]
                assert current["model_id"] == run["model_id"] == 1
                assert current["thread_id"] == original["thread_id"]
            else:
                assert current["model_id"] == 2 and current["thread_id"] != original["thread_id"]
                runs = (await client.get(f"{path}/runs", headers=alice)).json()["data"]
                assert not runs["items"]

    asyncio.run(exercise())


@pytest.mark.parametrize("timeout_sdk", ["openai", "anthropic", "httpx"])
def test_provider_timeout_does_not_poison_session_history(tmp_path: Path, timeout_sdk: str) -> None:
    """真实 SDK 超时连续发生后，同模型恢复仍能续聊并明确报告供应商超时。"""
    from anthropic import APITimeoutError as AnthropicTimeoutError
    from openai import APITimeoutError

    class ProviderTimeoutModel(SessionModel):
        """模拟真实记录中的 OpenAI SDK 超时异常。"""

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            """在图已保存用户输入后抛出供应商超时。"""
            request = httpx.Request("POST", "http://provider.test/v1/chat/completions")
            if timeout_sdk == "httpx":
                raise httpx.ReadTimeout("timeout", request=request)
            error = AnthropicTimeoutError if timeout_sdk == "anthropic" else APITimeoutError
            raise error(request=request)

    async def exercise() -> None:
        """复现成功、两次超时、再次成功的正式会话提交链路。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, session = await new_session(client, alice)
            first = (await submit(client, alice, path, "remember", "记住项目叫星河")).json()["data"]
            completed = await execute_text_run(sessions, first["id"], SessionModel(responses=[AIMessage(content="项目叫星河")]), tmp_path / "cp.db")
            assert completed.state == RunState.COMPLETED
            failed = []
            for key, text in [("timeout-one", "调用 current-time"), ("timeout-two", "？？？")]:
                response = await submit(client, alice, path, key, text)
                assert response.status_code == 201, response.text
                result = await execute_text_run(
                    sessions,
                    response.json()["data"]["id"],
                    ProviderTimeoutModel(responses=[AIMessage(content="unused")]),
                    tmp_path / "cp.db",
                )
                failed.append(result)
            SessionModel.seen_inputs.clear()
            response = await submit(client, alice, path, "continue", "项目叫什么？")
            assert response.status_code == 201, response.text
            recovered = await execute_text_run(sessions, response.json()["data"]["id"], SessionModel(responses=[AIMessage(content="星河")]), tmp_path / "cp.db")
            assert recovered.state == RunState.COMPLETED, recovered.failure_code
            assert recovered.thread_id == session["thread_id"]
            assert SessionModel.seen_inputs == ["记住项目叫星河", "项目叫什么？"]
            assert [(run.state, run.failure_code) for run in failed] == [(RunState.TIMED_OUT, "provider_timeout")] * 2

    asyncio.run(exercise())


def test_runtime_model_identity_overrides_inherited_assistant_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """切换后每轮系统提示均标明当前路由，避免继承历史助手的模型身份。"""
    captured = []
    original = SessionModel._agenerate

    async def capture(self, messages, stop=None, run_manager=None, **kwargs):
        """捕获真正发送到运行时模型的系统指令与历史。"""
        captured.append(messages)
        return await original(self, messages, stop=stop, run_manager=run_manager, **kwargs)

    monkeypatch.setattr(SessionModel, "_agenerate", capture)

    async def exercise() -> None:
        """保留旧身份回答后跨模型续聊，并验证现有线程也获得当前身份事实。"""
        async with model_environment(tmp_path) as (sessions, client, alice, _):
            path, _ = await new_session(client, alice)
            first = (await submit(client, alice, path, "identity-one", "你是谁？")).json()["data"]
            done = await execute_text_run(sessions, first["id"], SessionModel(responses=[AIMessage(content="我是旧模型 Model 1")]), tmp_path / "cp.db")
            assert done.state == RunState.COMPLETED
            session = (await client.get(path, headers=alice)).json()["data"]
            response = await client.put(
                path,
                headers=alice,
                json={
                    "expected_version": session["version"],
                    "model_id": 2,
                },
            )
            assert response.status_code == 200, response.text
            for key in ["identity-two", "identity-three"]:
                response = await submit(client, alice, path, key, "你是谁？")
                assert response.status_code == 201, response.text
                captured.clear()
                done = await execute_text_run(
                    sessions,
                    response.json()["data"]["id"],
                    SessionModel(responses=[AIMessage(content="当前接入 Model 2")]),
                    tmp_path / "cp.db",
                )
                assert done.state == RunState.COMPLETED
                messages = captured[-1]
                assert any(message.type == "ai" and message.content == "我是旧模型 Model 1" for message in messages)
                system = "\n".join(str(message.content) for message in messages if message.type == "system")
                assert '"model_id": 2' in system
                assert '"model_name": "Model 2"' in system
                assert '"external_id": "model-2"' in system
                assert "历史助手的自我介绍不能作为当前模型身份的依据" in system
                assert "帮助用户完成任务" in system

    asyncio.run(exercise())


def test_stored_model_http_timeout_recovery_and_switch(tmp_path: Path) -> None:
    """真实 HTTP 与 SDK 验证超时分类、同模型续聊及切换后的上游模型字段。"""
    import json

    from sqlalchemy import select

    from app.models.model_call_record import ModelCallRecord
    from app.services.run_dispatcher import dispatch_next_run

    async def exercise() -> None:
        """用本地 SSE 服务替代外部模型，不读取或调用真实供应商。"""
        requests = []
        handlers = set()

        async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """按调用顺序返回成功、无响应超时与成功的 OpenAI 兼容流。"""
            task = asyncio.current_task()
            handlers.add(task)
            try:
                header = await reader.readuntil(b"\r\n\r\n")
                length = next(int(line.split(b":", 1)[1]) for line in header.split(b"\r\n") if line.lower().startswith(b"content-length:"))
                body = json.loads(await reader.readexactly(length))
                requests.append(body)
                if len(requests) == 2:
                    await reader.read()
                    return
                chunk = {
                    "id": "local-chat",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "星河" if len(requests) != 4 else "当前接入 Model 2",
                            },
                            "finish_reason": None,
                        },
                    ],
                }
                data = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\ndata: [DONE]\n\n".encode()
                writer.write(
                    (f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode() + data,
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(upstream, "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            async with model_environment(tmp_path) as (sessions, client, alice, _):
                async with sessions.begin() as db:
                    for provider_id in (1, 2):
                        provider = await db.get(ProviderConnection, provider_id)
                        provider.base_url = f"http://127.0.0.1:{port}/v1"
                        provider.timeout_seconds = 1
                path, _ = await new_session(client, alice)
                results = []
                for key, text in [("http-one", "记住项目叫星河"), ("http-timeout", "获取时间"), ("http-recover", "项目叫什么？")]:
                    response = await submit(client, alice, path, key, text)
                    assert response.status_code == 201, response.text
                    results.append(await dispatch_next_run(sessions, tmp_path / "cp.db"))
                assert [(run.state, run.failure_code) for run in results] == [
                    (RunState.COMPLETED, None),
                    (RunState.TIMED_OUT, "provider_timeout"),
                    (RunState.COMPLETED, None),
                ]
                assert len({run.thread_id for run in results}) == 1
                content = json.dumps(requests[-1]["messages"], ensure_ascii=False)
                assert "记住项目叫星河" in content and "项目叫什么？" in content and "获取时间" not in content
                session = (await client.get(path, headers=alice)).json()["data"]
                response = await client.put(
                    path,
                    headers=alice,
                    json={
                        "expected_version": session["version"],
                        "model_id": 2,
                    },
                )
                assert response.status_code == 200, response.text
                response = await submit(client, alice, path, "http-switch", "你是谁？")
                assert response.status_code == 201, response.text
                switched = await dispatch_next_run(sessions, tmp_path / "cp.db")
                assert switched.state == RunState.COMPLETED and switched.model_id == 2
                assert [request["model"] for request in requests] == ["model-1", "model-1", "model-1", "model-2"]
                assert '"external_id": "model-2"' in requests[-1]["messages"][0]["content"]
                async with sessions() as db:
                    records = (await db.scalars(select(ModelCallRecord).order_by(ModelCallRecord.id))).all()
                    assert [record.external_id for record in records] == ["model-1", "model-1", "model-1", "model-2"]
                    assert records[1].failure == "provider_timeout" and records[1].available is False
        finally:
            server.close()
            await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

    asyncio.run(exercise())
