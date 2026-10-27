# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:23:43
# @Author  : yangyuexiong
# @File    : test_agent_sessions.py

"""验证会话隔离、运行快照与可回放消息历史。"""

import asyncio
from decimal import Decimal
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import httpx
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.password import hash_password
from app.api.v1 import artifacts as artifact_api
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_session import AgentSession
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.run_dispatcher import dispatch_next_run
from app.services.artifact_store import publish_pending_answers
from app.services import runtime_adapter as runtime_adapter_module
from app.services import run_dispatcher as run_dispatcher_module
from app.services import message_attachments as message_attachments_module


class SessionModel(FakeMessagesListChatModel):
    """支持 Agent 工具绑定接口的固定回答模型。"""

    seen_inputs: ClassVar[list[str]] = []

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """测试模型不使用工具，保留自身实例。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """记录实际传给模型的用户消息内容。"""
        self.seen_inputs.extend(str(message.content) for message in messages if message.type == "human")
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_session_migration(tmp_path: Path) -> None:
    """独立 SQLite 库验证会话和快照列升降级及无外键。"""
    database = tmp_path / "sessions-migration.db"
    env = os.environ.copy()
    env.update({"DB_BACKEND": "sqlite", "SQLITE_DATABASE": str(database)})
    root = Path(__file__).resolve().parents[1]
    for args in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head"), ("check",)):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True, text=True)

    async def check() -> None:
        """确认数据库表、列和 ORM 同步。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with engine.connect() as connection:
                def inspect_schema(sync_connection) -> None:
                    """读取 Alembic 已创建的会话与 Run 列。"""
                    schema = inspect(sync_connection)
                    assert {column.name for column in AgentSession.__table__.columns} == {
                        column["name"] for column in schema.get_columns("yp_agent_session")
                    }
                    assert {column.name for column in AgentRun.__table__.columns} == {
                        column["name"] for column in schema.get_columns("yp_agent_run")
                    }
                    assert not schema.get_foreign_keys("yp_agent_session")
                    assert {column.name for column in AgentMessageAttachment.__table__.columns} == {
                        column["name"] for column in schema.get_columns("yp_agent_message_attachment")
                    }
                    assert not schema.get_foreign_keys("yp_agent_message_attachment")

                await connection.run_sync(inspect_schema)
        finally:
            await engine.dispose()

    asyncio.run(check())


def test_session_run_and_history(tmp_path: Path, monkeypatch) -> None:
    """两个用户及两个会话隔离，提交快照、归档和消息游标可恢复。"""

    async def exercise() -> None:
        """使用独立数据库和正式 HTTP 路由完成两轮对话。"""
        prompts: list[str | None] = []
        generation_settings: list[tuple[Decimal | None, int | None]] = []
        original_create_agent = runtime_adapter_module.create_deep_agent
        original_build_model = run_dispatcher_module.build_chat_model

        def capture_prompt(*args, **kwargs):
            """记录运行时实际传给 DeepAgents 的系统指令。"""
            prompts.append(kwargs.get("system_prompt"))
            return original_create_agent(*args, **kwargs)

        monkeypatch.setattr(runtime_adapter_module, "create_deep_agent", capture_prompt)

        def capture_model(model, provider, **kwargs):
            """记录队列运行实际使用的生成参数快照。"""
            generation_settings.append((kwargs.get("temperature"), kwargs.get("max_output_tokens")))
            return original_build_model(model, provider, **kwargs)

        monkeypatch.setattr(run_dispatcher_module, "build_chat_model", capture_model)
        artifact_root = tmp_path / "artifacts"
        artifact_root.mkdir(mode=0o700)
        monkeypatch.setattr(artifact_api, "artifact_root", lambda: artifact_root)
        monkeypatch.setattr(message_attachments_module, "artifact_root", lambda: artifact_root)
        SessionModel.seen_inputs.clear()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all([
                    Admin(id=1, username="session-alice", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                    Admin(id=2, username="session-bob", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                    ProviderConnection(
                        id=1, name="session-provider", name_key="session-provider", kind="ollama",
                        base_url="http://localhost:11434/v1", creator_id=1, status=1, is_deleted=0,
                    ),
                    ModelCatalog(
                        id=1, provider_id=1, external_id="session-model", display_name="Session model",
                        capabilities=["text"], verified_capabilities=["text"], capabilities_verified=True,
                        context_window=8192, input_price=Decimal("1.5"), output_price=Decimal("2"),
                        creator_id=1, status=1, is_deleted=0,
                    ),
                    ModelCatalog(
                        id=2, provider_id=1, external_id="alternate-model", display_name="Alternate model",
                        capabilities=["text"], verified_capabilities=["text"], capabilities_verified=True,
                        context_window=8192, input_price=Decimal("1.5"), output_price=Decimal("2"),
                        creator_id=1, status=1, is_deleted=0,
                    ),
                ])

            async def test_session():
                """将请求会话绑定到临时数据库。"""
                async with sessions() as db:
                    yield db

            app.dependency_overrides[get_db_session] = test_session
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    async def login(username: str) -> dict[str, str]:
                        """登录测试用户并返回 Bearer 请求头。"""
                        response = await client.post("/api/v1/auth/login", json={
                            "identifier": username, "password": "StrongPassword123",
                        })
                        assert response.status_code == 200, response.text
                        return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}

                    alice, bob = await login("session-alice"), await login("session-bob")
                    created = await client.post("/api/v1/agents", headers=alice, json={
                        "name": "需求助手", "default_model_id": 1, "system_prompt": "只回答需求问题",
                    })
                    assert created.status_code == 201, created.text
                    agent_id = created.json()["data"]["id"]
                    base = f"/api/v1/agents/{agent_id}/sessions"
                    first = await client.post(base, headers=alice, json={"title": "第一会话"})
                    second = await client.post(base, headers=alice, json={"title": "第二会话"})
                    assert first.status_code == second.status_code == 201
                    first_id = first.json()["data"]["id"]
                    second_id = second.json()["data"]["id"]
                    first_thread = first.json()["data"]["thread_id"]
                    assert first_thread != second.json()["data"]["thread_id"]
                    assert (await client.get(base, headers=bob)).status_code == 404
                    for method, path, data in (
                        ("GET", f"{base}/{first_id}", None),
                        ("PUT", f"{base}/{first_id}", {"expected_version": 1, "title": "盗用"}),
                        ("DELETE", f"{base}/{first_id}", None),
                        ("POST", f"{base}/{first_id}/runs", {"request_key": "stolen", "input_text": "偷看"}),
                        ("GET", f"{base}/{first_id}/messages", None),
                    ):
                        response = await client.request(method, path, headers=bob, json=data)
                        assert response.status_code == 404, response.text
                    listed = (await client.get(f"{base}?search=第一", headers=alice)).json()["data"]
                    assert listed["total"] == 1 and listed["items"][0]["id"] == first_id

                    uploaded = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                        ("files", ("notes.txt", b"first attachment", "text/plain")),
                        ("files", ("other.txt", b"second attachment", "text/plain")),
                    ])
                    assert uploaded.status_code == 201, uploaded.text
                    artifact_ids = [item["id"] for item in uploaded.json()["data"]]
                    bob_upload = await client.post("/api/v1/artifacts/uploads", headers=bob, files=[
                        ("files", ("private.txt", b"bob only", "text/plain")),
                    ])
                    assert bob_upload.status_code == 201
                    bob_artifact = bob_upload.json()["data"][0]["id"]
                    large_upload = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                        ("files", ("large.txt", b"x" * (16 * 1024 + 1), "text/plain")),
                    ])
                    assert large_upload.status_code == 201
                    large_id = large_upload.json()["data"][0]["id"]
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "oversized-context", "input_text": "第一轮",
                        "artifact_ids": [large_id], "context_artifact_ids": [large_id],
                    })).status_code == 413
                    pdf_upload = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                        ("files", ("reference.pdf", b"%PDF-1.4\n%%EOF", "application/pdf")),
                    ])
                    assert pdf_upload.status_code == 201
                    pdf_id = pdf_upload.json()["data"][0]["id"]
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "unsupported-context", "input_text": "第一轮",
                        "artifact_ids": [pdf_id], "context_artifact_ids": [pdf_id],
                    })).status_code == 400
                    for bad_ids, expected_status in (
                        ([artifact_ids[0], artifact_ids[0]], 400),
                        ([bob_artifact], 404),
                        (["00000000-0000-0000-0000-000000000000"], 404),
                    ):
                        rejected = await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                            "request_key": "invalid-attachment", "input_text": "第一轮", "artifact_ids": bad_ids,
                        })
                        assert rejected.status_code == expected_status, rejected.text
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "invalid-context", "input_text": "第一轮",
                        "artifact_ids": [artifact_ids[0]], "context_artifact_ids": [artifact_ids[1]],
                    })).status_code == 400

                    first_run = await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "第一轮", "artifact_ids": artifact_ids,
                        "context_artifact_ids": [artifact_ids[0]], "publish_answer_artifact": True,
                    })
                    assert first_run.status_code == 201, first_run.text
                    first_run_id = first_run.json()["data"]["id"]
                    assert first_run.json()["data"]["agent_config_version"] == 1
                    assert first_run.json()["data"]["thread_id"] == first_thread
                    assert first_run.json()["data"]["publish_answer_artifact"] is True
                    repeated = await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "第一轮", "artifact_ids": artifact_ids,
                        "context_artifact_ids": [artifact_ids[0]], "publish_answer_artifact": True,
                    })
                    assert repeated.status_code == 201 and repeated.json()["data"]["id"] == first_run_id
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "第一轮", "artifact_ids": artifact_ids,
                        "context_artifact_ids": [artifact_ids[0]],
                    })).status_code == 409
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "不同内容", "artifact_ids": artifact_ids,
                        "context_artifact_ids": [artifact_ids[0]],
                    })).status_code == 409
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "第一轮", "artifact_ids": artifact_ids[::-1],
                    })).status_code == 409
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "第一轮", "artifact_ids": artifact_ids,
                    })).status_code == 409
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "too-early", "input_text": "下一轮",
                    })).status_code == 409
                    assert (await client.put(f"{base}/{first_id}", headers=alice, json={
                        "expected_version": 2, "archived": True,
                    })).status_code == 409
                    assert (await client.post("/api/v1/runs", headers=alice, json={
                        "model_id": 1, "thread_id": first_thread, "input_text": "绕过会话",
                    })).status_code == 409

                    async def model_one(db, run):
                        """首个会话运行返回固定回答。"""
                        await run_dispatcher_module.resolve_stored_model(db, run)
                        return SessionModel(responses=[AIMessage(content="第一答复")])

                    completed = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=model_one)
                    assert completed is not None and completed.id == first_run_id and completed.state == "completed"
                    assert prompts[-1] == "只回答需求问题"
                    assert "first attachment" in SessionModel.seen_inputs[-1]
                    assert "second attachment" not in SessionModel.seen_inputs[-1]
                    assert generation_settings[-1] == (Decimal("1.00"), 4096)
                    assert await publish_pending_answers(sessions, artifact_root) == 1
                    assert await publish_pending_answers(sessions, artifact_root) == 0
                    published = await client.get("/api/v1/artifacts", headers=alice, params={"run_id": first_run_id})
                    assert published.status_code == 200
                    answer = published.json()["data"]
                    assert len(answer) == 1 and answer[0]["filename"] == "answer.txt"
                    answer_id = answer[0]["id"]
                    downloaded = await client.get(f"/api/v1/artifacts/{answer_id}/download", headers=alice)
                    assert downloaded.status_code == 200 and downloaded.content == "第一答复".encode()
                    assert (await client.get("/api/v1/artifacts", headers=bob, params={"run_id": first_run_id})
                        ).status_code == 404
                    assert (await client.get(f"/api/v1/artifacts/{answer_id}/download", headers=bob)
                        ).status_code == 404
                    events = (await client.get(f"/api/v1/runs/{first_run_id}/events", headers=alice)).json()["data"]
                    assert [item["artifact_id"] for item in events if item["event_type"] == "artifact_published"] == [answer_id]
                    streamed = await client.get(f"/api/v1/runs/{first_run_id}/stream", headers=alice)
                    assert streamed.status_code == 200
                    assert "event: artifact_published" in streamed.text and answer_id in streamed.text
                    published_messages = (await client.get(f"{base}/{first_id}/messages", headers=alice)).json()["data"]
                    assert published_messages[0]["artifacts"] == []
                    assert published_messages[1]["artifacts"][0]["artifact_id"] == answer_id
                    assert published_messages[1]["artifacts"][0]["available"] is True
                    assert (await client.delete(f"/api/v1/artifacts/{answer_id}", headers=alice)).status_code == 200
                    assert (await client.get("/api/v1/artifacts", headers=alice, params={"run_id": first_run_id})
                        ).json()["data"] == []
                    assert (await client.get(f"/api/v1/artifacts/{answer_id}/download", headers=alice)
                        ).status_code == 404
                    messages = (await client.get(f"{base}/{first_id}/messages", headers=alice)).json()["data"]
                    assert [(item["role"], item["content"]) for item in messages] == [
                        ("user", "第一轮"), ("assistant", "第一答复"),
                    ]
                    assert [item["artifact_id"] for item in messages[0]["attachments"]] == artifact_ids
                    assert all(item["available"] for item in messages[0]["attachments"])
                    assert [item["included_in_context"] for item in messages[0]["attachments"]] == [True, False]
                    assert messages[1]["attachments"] == []
                    assert messages[1]["artifacts"][0]["artifact_id"] == answer_id
                    assert messages[1]["artifacts"][0]["available"] is False
                    run_messages = (await client.get(f"/api/v1/runs/{first_run_id}/messages", headers=alice)).json()["data"]
                    assert [item["artifact_id"] for item in run_messages[0]["attachments"]] == artifact_ids
                    assert run_messages[1]["artifacts"][0]["artifact_id"] == answer_id
                    assert (await client.get(f"{base}/{first_id}/messages?after={messages[0]['id']}", headers=alice)
                        ).json()["data"][0]["content"] == "第一答复"
                    assert (await client.get(f"{base}/{first_id}/messages", headers=bob)).status_code == 404
                    assert (await client.get(f"{base}/{second_id}/messages", headers=alice)).json()["data"] == []
                    assert (await client.delete(f"/api/v1/artifacts/{artifact_ids[0]}", headers=alice)).status_code == 200
                    stale = (await client.get(f"{base}/{first_id}/messages", headers=alice)).json()["data"][0]["attachments"]
                    assert [item["available"] for item in stale] == [False, True]
                    assert stale[0]["filename"] == "notes.txt"

                    edited = await client.put(f"/api/v1/agents/{agent_id}", headers=alice, json={
                        "expected_version": 1, "system_prompt": "使用新版指令",
                        "temperature": "0.25", "max_output_tokens": 2048,
                        "model_timeout_seconds": 60, "tool_timeout_seconds": 30,
                        "max_model_calls": 3, "max_tool_calls": 2,
                    })
                    assert edited.status_code == 200, edited.text
                    retry_after_edit = await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "first-turn", "input_text": "第一轮", "artifact_ids": artifact_ids,
                        "context_artifact_ids": [artifact_ids[0]], "publish_answer_artifact": True,
                    })
                    assert retry_after_edit.status_code == 201 and retry_after_edit.json()["data"]["id"] == first_run_id
                    next_run = await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "second-turn", "input_text": "第二轮",
                    })
                    assert next_run.status_code == 201, next_run.text
                    assert next_run.json()["data"]["agent_config_version"] == 2
                    assert next_run.json()["data"]["thread_id"] == first_thread
                    assert next_run.json()["data"]["model_timeout_seconds"] == 60
                    assert next_run.json()["data"]["tool_timeout_seconds"] == 30
                    assert next_run.json()["data"]["max_model_calls"] == 3
                    assert next_run.json()["data"]["max_tool_calls"] == 2
                    assert (await client.get(f"{base}/{first_id}/runs", headers=alice)).json()["data"]["total"] == 2
                    assert (await client.get(f"{base}/{second_id}/runs", headers=alice)).json()["data"]["total"] == 0
                    async with sessions() as db:
                        old = await db.get(AgentRun, first_run_id)
                        new = await db.get(AgentRun, next_run.json()["data"]["id"])
                        assert old.agent_system_prompt == "只回答需求问题"
                        assert new.agent_system_prompt == "使用新版指令"
                        assert old.agent_temperature == Decimal("1.00")
                        assert new.agent_temperature == Decimal("0.25")
                        assert old.agent_max_output_tokens == 4096
                        assert new.agent_max_output_tokens == 2048
                    other_run = await client.post(f"{base}/{second_id}/runs", headers=alice, json={
                        "request_key": "other-session", "input_text": "独立会话",
                    })
                    assert other_run.status_code == 201

                    async def model_two(db, run):
                        """不同会话按所属运行返回独立回答。"""
                        await run_dispatcher_module.resolve_stored_model(db, run)
                        answer = "第二答复" if run.session_id == first_id else "独立答复"
                        return SessionModel(responses=[AIMessage(content=answer)])

                    second_done = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=model_two)
                    assert second_done is not None and second_done.id == next_run.json()["data"]["id"]
                    assert second_done.state == "completed" and prompts[-1] == "使用新版指令"
                    assert generation_settings[-1] == (Decimal("0.25"), 2048)
                    other_done = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=model_two)
                    assert other_done is not None and other_done.id == other_run.json()["data"]["id"]
                    assert other_done.state == "completed" and prompts[-1] == "使用新版指令"
                    doomed_upload = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                        ("files", ("doomed.txt", b"must not reach model", "text/plain")),
                    ])
                    assert doomed_upload.status_code == 201
                    doomed_id = doomed_upload.json()["data"][0]["id"]
                    doomed_run = await client.post(f"{base}/{second_id}/runs", headers=alice, json={
                        "request_key": "doomed-turn", "input_text": "使用失效附件",
                        "artifact_ids": [doomed_id], "context_artifact_ids": [doomed_id],
                    })
                    assert doomed_run.status_code == 201
                    assert (await client.delete(f"/api/v1/artifacts/{doomed_id}", headers=alice)).status_code == 200
                    model_calls = len(SessionModel.seen_inputs)
                    failed = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=model_two)
                    assert failed is not None and failed.id == doomed_run.json()["data"]["id"]
                    assert failed.state == "failed" and failed.failure_code == "attachment_unavailable"
                    assert len(SessionModel.seen_inputs) == model_calls
                    tampered_upload = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                        ("files", ("tampered.txt", b"original content", "text/plain")),
                    ])
                    assert tampered_upload.status_code == 201
                    tampered_id = tampered_upload.json()["data"][0]["id"]
                    tampered_run = await client.post(f"{base}/{second_id}/runs", headers=alice, json={
                        "request_key": "tampered-turn", "input_text": "读取文件",
                        "artifact_ids": [tampered_id], "context_artifact_ids": [tampered_id],
                    })
                    assert tampered_run.status_code == 201
                    async with sessions() as db:
                        tampered = await db.get(Artifact, tampered_id)
                        (artifact_root / tampered.storage_key).write_bytes(b"changed! content")
                    failed_hash = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=model_two)
                    assert failed_hash is not None and failed_hash.id == tampered_run.json()["data"]["id"]
                    assert failed_hash.state == "failed" and failed_hash.failure_code == "attachment_unavailable"
                    assert len(SessionModel.seen_inputs) == model_calls
                    first_history = (await client.get(f"{base}/{first_id}/messages", headers=alice)).json()["data"]
                    other_history = (await client.get(f"{base}/{second_id}/messages", headers=alice)).json()["data"]
                    assert [item["content"] for item in first_history] == ["第一轮", "第一答复", "第二轮", "第二答复"]
                    assert [item["content"] for item in other_history] == [
                        "独立会话", "独立答复", "使用失效附件", "读取文件",
                    ]
                    assert other_history[-2]["attachments"][0]["available"] is False
                    budget_config = await client.put(f"/api/v1/agents/{agent_id}", headers=alice, json={
                        "expected_version": 2, "max_total_tokens": 10000, "max_estimated_cost": "0.010000",
                    })
                    assert budget_config.status_code == 200, budget_config.text
                    budget_run = await client.post(f"{base}/{second_id}/runs", headers=alice, json={
                        "request_key": "budgeted-turn", "input_text": "预算快照",
                    })
                    assert budget_run.status_code == 201, budget_run.text
                    budget_id = budget_run.json()["data"]["id"]
                    assert budget_run.json()["data"]["agent_config_version"] == 3
                    assert budget_run.json()["data"]["max_total_tokens"] == 10000
                    assert budget_run.json()["data"]["max_estimated_cost"] == "0.010000"
                    cleared = await client.put(f"/api/v1/agents/{agent_id}", headers=alice, json={
                        "expected_version": 3, "max_total_tokens": None, "max_estimated_cost": None,
                    })
                    assert cleared.status_code == 200
                    retried_budget = await client.post(f"{base}/{second_id}/runs", headers=alice, json={
                        "request_key": "budgeted-turn", "input_text": "预算快照",
                    })
                    assert retried_budget.status_code == 201 and retried_budget.json()["data"]["id"] == budget_id
                    async with sessions() as db:
                        saved_budget = await db.get(AgentRun, budget_id)
                        assert saved_budget.max_total_tokens == 10000
                        assert saved_budget.max_estimated_cost == Decimal("0.010000")
                        assert saved_budget.cost_input_price == Decimal("1.500000")
                        assert saved_budget.cost_output_price == Decimal("2.000000")
                    assert (await client.post(f"/api/v1/runs/{budget_id}/cancel", headers=alice)).status_code == 200
                    renamed = await client.put(f"{base}/{second_id}", headers=alice, json={
                        "expected_version": 5, "title": "已改名",
                    })
                    assert renamed.status_code == 200 and renamed.json()["data"]["title"] == "已改名"
                    archived = await client.put(f"{base}/{first_id}", headers=alice, json={
                        "expected_version": 3, "archived": True,
                    })
                    assert archived.status_code == 200 and archived.json()["data"]["archived"] is True
                    assert (await client.post(f"{base}/{first_id}/runs", headers=alice, json={
                        "request_key": "archived-turn", "input_text": "不能提交",
                    })).status_code == 409
                    assert (await client.get(f"{base}?archived=true", headers=alice)).json()["data"]["total"] == 1
                    assert (await client.delete(f"{base}/{first_id}", headers=alice)).status_code == 200
                    assert (await client.get(f"{base}/{first_id}", headers=alice)).status_code == 404
                    assert (await client.get(f"{base}/{second_id}", headers=alice)).status_code == 200
                    switched = await client.put(f"/api/v1/agents/{agent_id}", headers=alice, json={
                        "expected_version": 4, "default_model_id": 2,
                    })
                    assert switched.status_code == 200
                    incompatible = await client.post(f"{base}/{second_id}/runs", headers=alice, json={
                        "request_key": "model-switch", "input_text": "继续旧会话",
                    })
                    assert incompatible.status_code == 409
                    assert "创建新会话" in incompatible.json()["message"]
                    fresh = await client.post(base, headers=alice, json={"title": "切换模型后"})
                    assert fresh.status_code == 201
                    fresh_run = await client.post(f"{base}/{fresh.json()['data']['id']}/runs", headers=alice, json={
                        "request_key": "new-model", "input_text": "新的会话",
                    })
                    assert fresh_run.status_code == 201
                    assert fresh_run.json()["data"]["model_id"] == 2
            finally:
                app.dependency_overrides.pop(get_db_session, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
