# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:22:30
# @Author  : yangyuexiong
# @File    : test_run_api.py

"""验证公开文本运行接口的授权、状态和事件游标。"""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from app.core.config import get_config
from app.core.security import STREAM_COOKIE_NAME, STREAM_TICKET_SECONDS
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import runs as runs_api
from app.api.v1.runs import stream_events
from app.core.custom_exception import CustomException
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.base import shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.run import RunCreateIn
from app.services.artifact_store import publish_generated_artifact
from app.services.run_manager import (
    RunConflictError, append_text_delta, append_tool_event, claim_run, complete_run, create_approval_request, create_run,
    resume_approved_run,
)


def test_duplicate_submit_recovers_unique_constraint_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """并发提交键冲突后重新核对原请求，而不是把唯一约束错误暴露为 500。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'request-race.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="race-owner", password="not-used", role="user", status=1, is_deleted=0))
                db.add(ProviderConnection(
                    id=1, name="race-provider", name_key="race-provider", kind="ollama",
                    base_url="http://localhost:11434/v1", creator_id=1, status=1, is_deleted=0,
                ))
                db.add(ModelCatalog(
                    id=1, provider_id=1, external_id="race-model", display_name="race-model",
                    creator_id=1, status=1, is_deleted=0,
                ))
                existing = await create_run(db, 1, model_id=1, input_text="same", request_key="race")

            original = runs_api.create_run
            attempts = 0

            async def collided(*args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise IntegrityError("INSERT INTO yp_agent_run", {}, Exception("unique conflict"))
                return await original(*args, **kwargs)

            monkeypatch.setattr(runs_api, "create_run", collided)
            async with sessions() as db:
                response = await runs_api.submit_run(
                    RunCreateIn(model_id=1, input_text="same", request_key="race"),
                    actor=SimpleNamespace(id=1), db=db,
                )
                assert response.status_code == 201
                assert json.loads(response.body)["data"]["id"] == existing.id
            assert attempts == 2

            attempts = 0
            async with sessions() as db:
                with pytest.raises(CustomException) as error:
                    await runs_api.submit_run(
                        RunCreateIn(model_id=1, input_text="different", request_key="race"),
                        actor=SimpleNamespace(id=1), db=db,
                    )
                assert error.value.status_code == 409

            attempts = 0
            async with sessions() as db:
                with pytest.raises(CustomException) as error:
                    await runs_api.submit_run(
                        RunCreateIn(model_id=1, input_text="new", request_key="unrelated"),
                        actor=SimpleNamespace(id=1), db=db,
                    )
                assert error.value.status_code == 409
            assert attempts == 1

            attempts = 0

            async def always_collides(*args, **kwargs):
                raise IntegrityError("INSERT INTO secret_table", {"secret": "private"}, Exception("unique conflict"))

            monkeypatch.setattr(runs_api, "create_run", always_collides)
            async with sessions() as db:
                with pytest.raises(CustomException) as error:
                    await runs_api.submit_run(
                        RunCreateIn(model_id=1, input_text="same", request_key="race"),
                        actor=SimpleNamespace(id=1), db=db,
                    )
                assert error.value.status_code == 409
                assert "secret_table" not in str(error.value) and "private" not in str(error.value)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def exercise_run_api(database: Path) -> None:
    """通过真实路由验证运行创建、隔离、取消和终态回放。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as db:
        db.add_all([
            Admin(
                username="alice", mail="alice@example.com", password=hash_password("StrongPassword123"),
                role="user", status=1, is_deleted=0,
            ),
            Admin(username="bob", mail="bob@example.com", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
        ])
        db.add(ProviderConnection(
            name="local", name_key="local", kind="ollama", base_url="http://localhost:11434/v1",
            timeout_seconds=15, creator_id=1, status=1, is_deleted=0,
        ))
        await db.flush()
        db.add(ModelCatalog(
            provider_id=1, external_id="local-model", display_name="Local model", capabilities=["text"],
            verified_capabilities=[], capabilities_verified=False, is_default=False, creator_id=1,
            status=1, is_deleted=0,
        ))

    async def test_session():
        """为 HTTP 集成测试提供临时业务数据库。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def login(username: str) -> dict[str, str]:
                """登录指定测试用户并返回 Bearer 请求头。"""
                response = await client.post("/api/v1/auth/login", json={"identifier": username, "password": "StrongPassword123"})
                assert response.status_code == 200, response.text
                return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}

            alice = await login("alice")
            bob = await login("bob")
            assert (await client.post("/api/v1/runs", json={"model_id": 1, "input_text": "hi"})).status_code == 401
            assert (await client.post("/api/v1/runs", headers=alice, json={"model_id": 999, "input_text": "hi"})).status_code == 404
            created = await client.post(
                "/api/v1/runs", headers=alice, json={"model_id": 1, "input_text": "hi", "request_key": "once"},
            )
            assert created.status_code == 201, created.text
            run = created.json()["data"]
            run_id = run["id"]
            assert run["state"] == "queued" and run["event_seq"] == 1
            assert run["model_timeout_seconds"] == 120
            assert run["tool_timeout_seconds"] == 120
            assert run["max_queue_wait_seconds"] is None and run["queue_expires_at"] is None
            assert run["enable_text_artifact_tool"] is False
            assert run["publish_answer_artifact"] is False
            assert run["max_model_calls"] is None and run["model_call_count"] == 0
            assert run["max_total_tokens"] is None and run["reported_total_tokens"] == 0
            assert run["max_estimated_cost"] is None and run["estimated_cost"] == "0"
            assert run["cost_currency"] is None
            assert run["max_tool_calls"] is None and run["tool_call_count"] == 0
            assert "lease_token" not in run and "checkpoint_id" not in run and "owner_id" not in run
            duplicate = await client.post(
                "/api/v1/runs", headers=alice, json={"model_id": 1, "input_text": "hi", "request_key": "once"},
            )
            assert duplicate.status_code == 201 and duplicate.json()["data"]["id"] == run_id
            timeout_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "model_timeout_seconds": 60},
            )
            assert timeout_conflict.status_code == 409
            tool_timeout_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "tool_timeout_seconds": 30},
            )
            assert tool_timeout_conflict.status_code == 409
            queue_timeout_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "max_queue_wait_seconds": 30},
            )
            assert queue_timeout_conflict.status_code == 409
            tool_config_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "enable_text_artifact_tool": True},
            )
            assert tool_config_conflict.status_code == 409
            budget_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "max_model_calls": 2},
            )
            assert budget_conflict.status_code == 409
            token_budget_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "max_total_tokens": 100},
            )
            assert token_budget_conflict.status_code == 409
            cost_budget_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "max_estimated_cost": "0.010000"},
            )
            assert cost_budget_conflict.status_code == 409
            tool_budget_conflict = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "request_key": "once", "max_tool_calls": 2},
            )
            assert tool_budget_conflict.status_code == 409
            for invalid_budget in (0, 101):
                for field in ("max_model_calls", "max_tool_calls"):
                    invalid = await client.post(
                        "/api/v1/runs", headers=alice,
                        json={"model_id": 1, "input_text": "hi", field: invalid_budget},
                    )
                    assert invalid.status_code == 400
            for invalid_tokens in (0, 10000001):
                invalid = await client.post(
                    "/api/v1/runs", headers=alice,
                    json={"model_id": 1, "input_text": "hi", "max_total_tokens": invalid_tokens},
                )
                assert invalid.status_code == 400
            for invalid_cost in ("0", "-1", "0.0000001", "1000000000000"):
                invalid = await client.post(
                    "/api/v1/runs", headers=alice,
                    json={"model_id": 1, "input_text": "hi", "max_estimated_cost": invalid_cost},
                )
                assert invalid.status_code == 400
            unpriced = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "hi", "max_estimated_cost": "0.010000"},
            )
            assert unpriced.status_code == 409
            async with sessions.begin() as db:
                model = await db.get(ModelCatalog, 1)
                model.input_price = Decimal("2")
                model.output_price = Decimal("4")
            for invalid_timeout in (0, 121):
                for field in ("model_timeout_seconds", "tool_timeout_seconds"):
                    invalid = await client.post(
                        "/api/v1/runs", headers=alice,
                        json={"model_id": 1, "input_text": "hi", field: invalid_timeout},
                    )
                    assert invalid.status_code == 400
            for invalid_wait in (0, 86401):
                invalid = await client.post(
                    "/api/v1/runs", headers=alice,
                    json={"model_id": 1, "input_text": "hi", "max_queue_wait_seconds": invalid_wait},
                )
                assert invalid.status_code == 400
            bounded = await client.post(
                "/api/v1/runs", headers=alice,
                json={
                    "model_id": 1, "input_text": "bounded", "model_timeout_seconds": 60,
                    "tool_timeout_seconds": 30, "max_queue_wait_seconds": 300,
                },
            )
            assert bounded.status_code == 201 and bounded.json()["data"]["model_timeout_seconds"] == 60
            assert bounded.json()["data"]["tool_timeout_seconds"] == 30
            assert bounded.json()["data"]["max_queue_wait_seconds"] == 300
            assert bounded.json()["data"]["queue_expires_at"] is not None
            enabled_tool = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "读取我的文本文件", "enable_text_artifact_tool": True},
            )
            assert enabled_tool.status_code == 409
            async with sessions.begin() as db:
                model = await db.get(ModelCatalog, 1)
                model.verified_capabilities = ["tools"]
            enabled_tool = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "读取我的文本文件", "enable_text_artifact_tool": True,
                      "request_key": "artifact-enabled"},
            )
            assert enabled_tool.status_code == 201
            assert enabled_tool.json()["data"]["enable_text_artifact_tool"] is True
            disabled_duplicate = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "读取我的文本文件", "request_key": "artifact-enabled"},
            )
            assert disabled_duplicate.status_code == 409
            answer_export = await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "导出回答", "publish_answer_artifact": True,
                      "request_key": "answer-export"},
            )
            assert answer_export.status_code == 201
            assert answer_export.json()["data"]["publish_answer_artifact"] is True
            assert (await client.post(
                "/api/v1/runs", headers=alice,
                json={"model_id": 1, "input_text": "导出回答", "request_key": "answer-export"},
            )).status_code == 409
            answer_id = answer_export.json()["data"]["id"]
            async with sessions.begin() as db:
                claimed = await claim_run(db, answer_id)
                await complete_run(db, answer_id, claimed.lease_token, final_text="导出的正文")
            artifact_root = database.parent / "answer-output"
            artifact_root.mkdir(mode=0o700)
            answer_artifact = await publish_generated_artifact(
                sessions, answer_id, "final-answer-v1", "answer.txt", "导出的正文".encode(), artifact_root,
            )
            answer_events = await client.get(f"/api/v1/runs/{answer_id}/stream", headers=alice)
            assert answer_events.status_code == 200
            assert "event: artifact_published" in answer_events.text
            assert answer_artifact.id in answer_events.text
            bounded_id = bounded.json()["data"]["id"]
            assert (await client.get(f"/api/v1/runs/{bounded_id}", headers=alice)).json()["data"]["model_timeout_seconds"] == 60
            assert (await client.get(f"/api/v1/runs/{bounded_id}", headers=alice)).json()["data"]["tool_timeout_seconds"] == 30
            budgeted = await client.post(
                "/api/v1/runs", headers=alice,
                json={
                    "model_id": 1, "input_text": "limited", "request_key": "limited",
                    "max_model_calls": 2, "max_tool_calls": 3, "max_total_tokens": 1000,
                    "max_estimated_cost": "0.010000",
                },
            )
            assert budgeted.status_code == 201 and budgeted.json()["data"]["max_model_calls"] == 2
            assert budgeted.json()["data"]["model_call_count"] == 0
            assert budgeted.json()["data"]["max_tool_calls"] == 3 and budgeted.json()["data"]["tool_call_count"] == 0
            assert budgeted.json()["data"]["max_total_tokens"] == 1000
            assert budgeted.json()["data"]["reported_total_tokens"] == 0
            assert budgeted.json()["data"]["max_estimated_cost"] == "0.010000"
            assert budgeted.json()["data"]["estimated_cost"] == "0"
            assert budgeted.json()["data"]["cost_currency"] == "USD"
            repeated_budget = await client.post(
                "/api/v1/runs", headers=alice,
                json={
                    "model_id": 1, "input_text": "limited", "request_key": "limited",
                    "max_model_calls": 2, "max_tool_calls": 3, "max_total_tokens": 1000,
                    "max_estimated_cost": "0.010000",
                },
            )
            assert repeated_budget.status_code == 201 and repeated_budget.json()["data"]["id"] == budgeted.json()["data"]["id"]
            conflict = await client.post(
                "/api/v1/runs", headers=alice, json={"model_id": 1, "input_text": "different", "request_key": "once"},
            )
            assert conflict.status_code == 409
            concurrent_thread = await client.post(
                "/api/v1/runs", headers=alice, json={"model_id": 1, "input_text": "next", "thread_id": run["thread_id"]},
            )
            assert concurrent_thread.status_code == 409
            for suffix in ("", "/messages", "/events", "/stream"):
                assert (await client.get(f"/api/v1/runs/{run_id}{suffix}", headers=bob)).status_code == 404
            assert (await client.post(f"/api/v1/runs/{run_id}/cancel", headers=bob)).status_code == 404
            messages = (await client.get(f"/api/v1/runs/{run_id}/messages", headers=alice)).json()["data"]
            assert [(message["role"], message["content"]) for message in messages] == [("user", "hi")]

            async with sessions.begin() as db:
                claimed = await claim_run(db, run_id)
                await append_text_delta(db, run_id, claimed.lease_token, "hello")
                tool_call_id = str(uuid4())
                await append_tool_event(db, run_id, claimed.lease_token, "tool_started", tool_call_id, "echo_approved")
                await append_tool_event(db, run_id, claimed.lease_token, "tool_completed", tool_call_id, "echo_approved")
                await complete_run(db, run_id, claimed.lease_token, checkpoint_id="checkpoint-1", final_text="hello")
            events = (await client.get(f"/api/v1/runs/{run_id}/events?after=1", headers=alice)).json()["data"]
            assert [(event["sequence"], event["event_type"]) for event in events] == [
                (2, "state"), (3, "text_delta"), (4, "tool_started"), (5, "tool_completed"), (6, "state"),
            ]
            assert events[1]["content"] == "hello"
            assert events[2]["tool_call_id"] == events[3]["tool_call_id"] == tool_call_id
            assert events[2]["tool_name"] == events[3]["tool_name"] == "echo_approved"
            assert events[2]["content"] is None and events[3]["content"] is None
            assert (await client.get(f"/api/v1/runs/{run_id}/events?after=6", headers=alice)).json()["data"] == []
            snapshot_url = f"/api/v1/runs/{run_id}/snapshot"
            assert (await client.get(snapshot_url, headers=bob)).status_code == 404
            first_snapshot = await client.get(f"{snapshot_url}?limit=2", headers=alice)
            assert first_snapshot.status_code == 200
            assert first_snapshot.headers["cache-control"] == "no-store"
            first_page = first_snapshot.json()["data"]
            assert first_page["run"]["state"] == "completed"
            assert first_page["latest_sequence"] == 6
            assert first_page["next_after"] == 2 and first_page["has_more"] is True
            assert [item["sequence"] for item in first_page["events"]] == [1, 2]
            second_page = (await client.get(f"{snapshot_url}?after=2&limit=2", headers=alice)).json()["data"]
            third_page = (await client.get(f"{snapshot_url}?after=4&limit=2", headers=alice)).json()["data"]
            assert [item["sequence"] for item in second_page["events"]] == [3, 4]
            assert second_page["next_after"] == 4 and second_page["has_more"] is True
            assert [item["sequence"] for item in third_page["events"]] == [5, 6]
            assert third_page["next_after"] == 6 and third_page["has_more"] is False
            caught_up = (await client.get(f"{snapshot_url}?after=6", headers=alice)).json()["data"]
            assert caught_up["events"] == [] and caught_up["next_after"] == 6
            assert caught_up["has_more"] is False
            assert (await client.get(f"{snapshot_url}?after=7", headers=alice)).status_code == 400
            stream = await client.get(f"/api/v1/runs/{run_id}/stream", headers={**alice, "Last-Event-ID": f"{run_id}:1"})
            assert stream.status_code == 200 and stream.headers["content-type"].startswith("text/event-stream")
            assert all(f"id: {run_id}:{sequence}" in stream.text for sequence in range(2, 7))
            assert "event: text_delta" in stream.text and '"content": "hello"' in stream.text
            assert "event: tool_started" in stream.text and "event: tool_completed" in stream.text
            assert tool_call_id in stream.text and "echo_approved" in stream.text
            assert f"id: {run_id}:1" not in stream.text
            assert (await client.get(f"/api/v1/runs/{run_id}/stream", headers={**alice, "Last-Event-ID": "other:1"})).status_code == 400
            assert (await client.get(f"/api/v1/runs/{run_id}/stream", headers={**alice, "Last-Event-ID": f"{run_id}:7"})).status_code == 400
            assert (await client.get(f"/api/v1/runs/{run_id}/stream", headers={**alice, "Last-Event-ID": f"{run_id}:{'9' * 5000}"})).status_code == 400
            assert (await client.post(f"/api/v1/runs/{run_id}/stream-ticket", headers=bob)).status_code == 404
            assert (await client.post(f"/api/v1/runs/{run_id}/stream-ticket")).status_code == 401
            ticket_response = await client.post(f"/api/v1/runs/{run_id}/stream-ticket", headers=alice)
            assert ticket_response.status_code == 200
            assert ticket_response.json()["data"]["expires_in"] == STREAM_TICKET_SECONDS
            assert ticket_response.headers["cache-control"] == "no-store"
            cookie_header = ticket_response.headers["set-cookie"]
            assert "HttpOnly" in cookie_header and "SameSite=strict" in cookie_header
            assert f"Path=/api/v1/runs/{run_id}/stream" in cookie_header
            stream_ticket = ticket_response.cookies[STREAM_COOKIE_NAME]
            cookie_stream = await client.get(f"/api/v1/runs/{run_id}/stream", headers={"Last-Event-ID": f"{run_id}:1"})
            assert cookie_stream.status_code == 200 and "event: text_delta" in cookie_stream.text
            assert (await client.get(f"/api/v1/runs/{uuid4()}/stream", headers={
                "Cookie": f"{STREAM_COOKIE_NAME}={stream_ticket}",
            })).status_code == 401
            expired_claims = jwt.decode(stream_ticket, options={"verify_signature": False})
            expired_claims["exp"] = 1
            expired_ticket = jwt.encode(expired_claims, get_config().SECRET_KEY, algorithm="HS256")
            assert (await client.get(f"/api/v1/runs/{run_id}/stream", headers={
                "Cookie": f"{STREAM_COOKIE_NAME}={expired_ticket}",
            })).status_code == 401
            messages = (await client.get(f"/api/v1/runs/{run_id}/messages", headers=alice)).json()["data"]
            assert [(message["role"], message["content"]) for message in messages] == [("user", "hi"), ("assistant", "hello")]

            queued = await client.post("/api/v1/runs", headers=alice, json={"model_id": 1, "input_text": "stop"})
            cancelled = await client.post(f"/api/v1/runs/{queued.json()['data']['id']}/cancel", headers=alice)
            assert cancelled.status_code == 200 and cancelled.json()["data"]["state"] == "cancelled"
            assert (await client.post(f"/api/v1/runs/{queued.json()['data']['id']}/cancel", headers=alice)).status_code == 200

            pending = await client.post("/api/v1/runs", headers=alice, json={"model_id": 1, "input_text": "approve tool"})
            pending_id = pending.json()["data"]["id"]
            assert (await client.get(f"/api/v1/runs/{pending_id}/approval", headers=alice)).status_code == 404
            async with sessions.begin() as db:
                claimed = await claim_run(db, pending_id)
                await create_approval_request(
                    db, pending_id, claimed.lease_token, "checkpoint-approval",
                    [{"name": "search", "arguments_hash": "a" * 64}],
                )
            assert (await client.get(f"/api/v1/runs/{pending_id}/approval", headers=bob)).status_code == 404
            assert (await client.post(
                f"/api/v1/runs/{pending_id}/approval/decision", headers=bob, json={"decision": "approve"},
            )).status_code == 404
            approval = (await client.get(f"/api/v1/runs/{pending_id}/approval", headers=alice)).json()["data"]
            assert approval["state"] == "pending" and approval["actions"] == [
                {"name": "search", "arguments_hash": "a" * 64, "resource_id": None, "resource_name": None},
            ]
            assert "checkpoint_id" not in approval and "owner_id" not in approval
            async with sessions.begin() as db:
                with pytest.raises(RunConflictError):
                    await resume_approved_run(db, pending_id)
            decision = await client.post(
                f"/api/v1/runs/{pending_id}/approval/decision", headers=alice, json={"decision": "reject"},
            )
            assert decision.status_code == 200 and decision.json()["data"]["state"] == "rejected"
            rejected_run = (await client.get(f"/api/v1/runs/{pending_id}", headers=alice)).json()["data"]
            assert rejected_run["state"] == "cancelled" and rejected_run["failure_code"] == "approval_rejected"
            rejected_events = (await client.get(f"/api/v1/runs/{pending_id}/events", headers=alice)).json()["data"]
            assert rejected_events[-1]["state"] == "cancelled"
            assert rejected_events[-1]["failure_code"] == "approval_rejected"
            async with sessions.begin() as db:
                with pytest.raises(RunConflictError):
                    await resume_approved_run(db, pending_id)
            repeated = await client.post(
                f"/api/v1/runs/{pending_id}/approval/decision", headers=alice, json={"decision": "approve"},
            )
            assert repeated.status_code == 409

            resource_id = str(uuid4())
            async with sessions.begin() as db:
                db.add(Artifact(
                    id=resource_id, owner_id=1, filename="approved.txt", media_type="text/plain", byte_size=4,
                    sha256="a" * 64, storage_key=uuid4().hex, source="upload", state="ready",
                    expires_at=shanghai_now(naive=True) + timedelta(days=1), status=1, is_deleted=0,
                ))
                enabled_id = enabled_tool.json()["data"]["id"]
                claimed = await claim_run(db, enabled_id)
                await create_approval_request(
                    db, enabled_id, claimed.lease_token, "checkpoint-file",
                    [{"name": "read_text_artifact", "arguments_hash": "b" * 64, "resource_id": resource_id}],
                )
            preview = (await client.get(f"/api/v1/runs/{enabled_id}/approval", headers=alice)).json()["data"]
            assert preview["actions"][0] == {
                "name": "read_text_artifact", "arguments_hash": "b" * 64,
                "resource_id": resource_id, "resource_name": "approved.txt",
            }
            assert (await client.get(f"/api/v1/runs/{enabled_id}/approval", headers=bob)).status_code == 404

            claims = jwt.decode(alice["Authorization"].split()[1], options={"verify_signature": False})
            async with sessions() as stream_db:
                stream_actor = SimpleNamespace(id=1)
                expired_request = SimpleNamespace(
                    state=SimpleNamespace(
                        auth_session_id=claims["sid"], auth_token_version=claims["ver"], auth_token_expires_at=0,
                    ),
                    is_disconnected=AsyncMock(return_value=False),
                )
                expired_stream = await stream_events(
                    bounded_id, expired_request, after=0, last_event_id=None, actor=stream_actor, db=stream_db,
                )
                with pytest.raises(StopAsyncIteration):
                    await anext(expired_stream.body_iterator)

                live_request = SimpleNamespace(
                    state=SimpleNamespace(
                        auth_session_id=claims["sid"], auth_token_version=claims["ver"],
                        auth_token_expires_at=claims["exp"],
                    ),
                    is_disconnected=AsyncMock(return_value=False),
                )
                live_stream = await stream_events(
                    bounded_id, live_request, after=0, last_event_id=None, actor=stream_actor, db=stream_db,
                )
                assert f"id: {bounded_id}:1" in await anext(live_stream.body_iterator)
                async with sessions.begin() as db:
                    await db.execute(update(Admin).where(Admin.id == 1).values(token_version=claims["ver"] + 1))
                with pytest.raises(StopAsyncIteration):
                    await anext(live_stream.body_iterator)
            assert (await client.get(f"/api/v1/runs/{run_id}/stream", headers={
                "Cookie": f"{STREAM_COOKIE_NAME}={stream_ticket}",
            })).status_code == 401
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        await engine.dispose()


def test_run_api(tmp_path: Path) -> None:
    """运行公开接口的完整 HTTP 集成场景。"""
    asyncio.run(exercise_run_api(tmp_path / "run-api.db"))
