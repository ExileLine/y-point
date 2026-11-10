# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 01:03:28
# @Author  : yangyuexiong
# @File    : test_run_calls.py

"""验证调用摘要的真实事件、状态边界和授权过滤。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import RunCallStatus, RunEventType, RunState
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.agent_share import AgentShare
from app.models.base import Base
from app.models.knowledge import KnowledgeBase, KnowledgeShare, RunKnowledgeBase
from app.services.run_calls import load_run_calls
from app.services.run_manager import create_run


def add_event(db, run, call_id: str, name: str, event_type: RunEventType) -> None:
    """向隔离测试库写入带连续序号的公开工具事件。"""
    run.event_seq += 1
    db.add(
        AgentRunEvent(
            run_id=run.id,
            sequence=run.event_seq,
            protocol_version=1,
            event_type=event_type,
            state=RunState.RUNNING,
            tool_call_id=call_id,
            tool_name=name,
            content="不应公开的参数和正文",
        ),
    )


def test_call_summary_status_names_and_permissions(tmp_path: Path) -> None:
    """实际调用才展示，保留重复调用结果，知识库撤权不公开名称。"""

    async def exercise() -> None:
        """使用隔离数据库检查历史调用与当前知识库访问权。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'calls.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="caller", password="unused-test-hash", status=1, is_deleted=0),
                        Admin(id=2, username="other", password="unused-test-hash", status=1, is_deleted=0),
                    ],
                )
                db.add_all(
                    [
                        KnowledgeBase(id=1, owner_id=1, name="差旅制度"),
                        KnowledgeBase(id=2, owner_id=1, name="没有索引的知识库"),
                        KnowledgeBase(id=3, owner_id=2, name="不得公开的知识库"),
                        KnowledgeBase(id=4, owner_id=2, name="共享规则"),
                    ],
                )
                share = KnowledgeShare(knowledge_base_id=4, user_id=1)
                db.add(share)
                run = await create_run(db, 1)
                untouched = await create_run(db, 1)
                active = await create_run(db, 1)
                foreign = await create_run(db, 2)
                run.state = RunState.FAILED
                active.state = RunState.RUNNING
                for identifier, index_id in [(1, 10), (2, None), (3, 30), (4, 40)]:
                    db.add(RunKnowledgeBase(run_id=run.id, knowledge_base_id=identifier, index_id=index_id))
                db.add(RunKnowledgeBase(run_id=untouched.id, knowledge_base_id=1, index_id=10))
                for name, result in [
                    ("skill:travel-policy@v2", RunEventType.TOOL_COMPLETED),
                    ("user_tool_8_calculate_cost", RunEventType.TOOL_FAILED),
                    ("user_tool_8_calculate_cost", RunEventType.TOOL_COMPLETED),
                    ("search_knowledge_base", RunEventType.TOOL_COMPLETED),
                    ("list_session_artifacts", None),
                ]:
                    call_id = str(uuid4())
                    add_event(db, run, call_id, name, RunEventType.TOOL_STARTED)
                    if result:
                        add_event(db, run, call_id, name, result)
                add_event(db, active, str(uuid4()), "skill:live@v1", RunEventType.TOOL_STARTED)
                add_event(db, foreign, str(uuid4()), "private-tool", RunEventType.TOOL_FAILED)
            async with sessions() as db:
                calls = await load_run_calls(db, 1, [run.id, untouched.id, active.id, foreign.id])
                rows = calls[run.id]
                assert [row.name for row in rows] == [
                    "travel-policy@v2",
                    "calculate_cost",
                    "calculate_cost",
                    "差旅制度、知识库（已不可用）、共享规则",
                    "list_session_artifacts",
                ]
                assert [row.status for row in rows] == [
                    RunCallStatus.SUCCEEDED,
                    RunCallStatus.FAILED,
                    RunCallStatus.SUCCEEDED,
                    RunCallStatus.SUCCEEDED,
                    RunCallStatus.INTERRUPTED,
                ]
                assert [row.kind for row in rows] == ["skill", "tool", "tool", "knowledge", "tool"]
                assert len({row.call_id for row in rows}) == 5
                assert [row.sequence for row in rows] == sorted(row.sequence for row in rows)
                assert untouched.id not in calls
                assert foreign.id not in calls
                assert calls[active.id][0].status == RunCallStatus.RUNNING
                assert "不应公开" not in str([row.model_dump() for row in rows])
                assert await load_run_calls(db, 1, []) == {}
            async with sessions.begin() as db:
                record = await db.get(KnowledgeShare, share.id)
                record.is_deleted = 1
            async with sessions() as db:
                calls = await load_run_calls(db, 1, [run.id])
                assert "共享规则" not in calls[run.id][3].name
                assert "差旅制度" in calls[run.id][3].name
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_call_api_and_session_history_authorization(tmp_path: Path, monkeypatch) -> None:
    """正式路由隔离运行所有者，消息批量携带调用，Agent 撤权立即拒绝。"""

    async def exercise() -> None:
        """通过真实 HTTP 路由检查摘要及无最终回答轮次的恢复数据。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'call-api.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor_id = 1
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="caller", password="unused-test-hash", status=1, is_deleted=0),
                        Admin(id=2, username="other", password="unused-test-hash", status=1, is_deleted=0),
                    ],
                )
                db.add(AgentConfig(id=1, owner_id=2, name="共享助手", default_model_id=1))
                share = AgentShare(agent_id=1, user_id=1)
                db.add(share)
                session = AgentSession(id=str(uuid4()), owner_id=1, agent_id=1, thread_id=str(uuid4()), title="调用历史")
                db.add(session)
                run = await create_run(db, 1, input_text="请检索")
                run.agent_id = 1
                run.session_id = session.id
                run.state = RunState.COMPLETED
                call_id = str(uuid4())
                add_event(db, run, call_id, "skill:policy@v1", RunEventType.TOOL_STARTED)
                add_event(db, run, call_id, "skill:policy@v1", RunEventType.TOOL_COMPLETED)
                db.add(AgentRunMessage(run_id=run.id, owner_id=1, role="assistant", content="完成"))
                failed = await create_run(db, 1, input_text="失败轮次")
                failed.agent_id = 1
                failed.session_id = session.id
                failed.state = RunState.FAILED
                add_event(db, failed, str(uuid4()), "user_tool_1_broken", RunEventType.TOOL_FAILED)

            async def database():
                """将正式 API 绑定到本测试的隔离数据库。"""
                async with sessions() as db:
                    yield db

            async def actor():
                """仅替换登录身份，保留业务层所有权与 Agent 授权检查。"""
                return Admin(id=actor_id, username="caller")

            monkeypatch.setitem(app.dependency_overrides, get_db_session, database)
            monkeypatch.setitem(app.dependency_overrides, get_current_admin, actor)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get(f"/api/v1/runs/{run.id}/calls")
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "no-store"
                row = response.json()["data"][0]
                assert row["name"] == "policy@v1" and row["status"] == "succeeded"
                assert set(row) == {"call_id", "kind", "name", "status", "sequence"}
                history = await client.get(f"/api/v1/agents/1/sessions/{session.id}/messages")
                assert history.status_code == 200, history.text
                messages = history.json()["data"]
                assert len(messages) == 3
                assert all(message["calls"] for message in messages)
                assert messages[-1]["role"] == "user" and messages[-1]["calls"][0]["status"] == "failed"
                actor_id = 2
                assert (await client.get(f"/api/v1/runs/{run.id}/calls")).status_code == 404
                actor_id = 1
                async with sessions.begin() as db:
                    await db.delete(await db.get(AgentShare, share.id))
                assert (await client.get(f"/api/v1/runs/{run.id}/calls")).status_code == 404
                assert (await client.get(f"/api/v1/agents/1/sessions/{session.id}/messages")).status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(exercise())
