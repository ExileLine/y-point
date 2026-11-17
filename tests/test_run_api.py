# -*- coding: utf-8 -*-
"""验证唯一 AG-UI 对话入口之外的运行资源、观察授权和游标约束。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest
from sqlalchemy import update

from app.api.v1.runs import stream_events
from app.core.config import get_config
from app.core.security import STREAM_COOKIE_NAME
from app.main import app
from app.models.admin import Admin
from app.services.run_manager import append_text_delta, claim_run, complete_run, create_run
from tests.test_ag_ui import environment, sse_events


async def completed_run(sessions):
    """持久化带完整 Agent 快照和交付屏障的已完成运行。"""
    async with sessions.begin() as db:
        run = await create_run(
            db,
            1,
            model_id=1,
            agent_id=1,
            session_id="session",
            input_text="公开问题",
            agent_config_version=1,
            agent_system_prompt="",
            agent_temperature=0.1,
            thread_id="checkpoint-thread",
            agent_max_output_tokens=None,
        )
        claimed = await claim_run(db, run.id)
        await append_text_delta(db, run.id, claimed.lease_token, "公开回答")
        await complete_run(db, run.id, claimed.lease_token, final_text="公开回答")
        return run.id


def test_only_ag_ui_conversation_routes_are_exposed() -> None:
    """OpenAPI 不再公开旧提交、审批决定、原始事件或第二套历史接口。"""
    paths = app.openapi()["paths"]
    assert "post" in paths["/api/v1/agents/{agent_id}/ag-ui"]
    assert "get" in paths["/api/v1/agents/{agent_id}/ag-ui/threads/{session_id}"]
    assert "/api/v1/runs" not in paths
    assert "post" not in paths["/api/v1/agents/{agent_id}/sessions/{session_id}/runs"]
    for path in (
        "/api/v1/runs/{run_id}/approval/decision",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/runs/{run_id}/snapshot",
        "/api/v1/agents/{agent_id}/sessions/{session_id}/messages",
    ):
        assert path not in paths
    assert "protocol" not in {parameter["name"] for parameter in paths["/api/v1/runs/{run_id}/stream"]["get"]["parameters"]}


@pytest.mark.parametrize("secure", [False, True])
def test_stream_ticket_scope_and_resource_authorization(tmp_path: Path, monkeypatch, secure: bool) -> None:
    """验证票据 Cookie 的安全属性、运行范围及资源访问隔离。"""
    monkeypatch.setattr(get_config(), "STREAM_COOKIE_SECURE", secure)

    async def exercise():
        """在真实登录和隔离业务库中执行资源访问场景。"""
        async with environment(tmp_path) as (sessions, client, headers):
            run_id = await completed_run(sessions)
            base = f"/api/v1/runs/{run_id}"
            other = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "ag-ui-other",
                    "password": "StrongPassword123",
                },
            )
            outsider = {
                "Authorization": f"Bearer {other.json()['data']['access_token']}",
            }
            for suffix in ("", "/messages", "/calls", "/approval", "/ag-ui/snapshot", "/stream"):
                assert (await client.get(base + suffix, headers=outsider)).status_code == 404
            assert (await client.post(base + "/cancel", headers=outsider)).status_code == 404
            assert (await client.post(base + "/stream-ticket", headers=outsider)).status_code == 404
            assert (await client.get(base)).status_code == 401
            assert (
                await client.get(
                    base + "/stream",
                    params={
                        "access_token": headers["Authorization"].split()[1],
                    },
                )
            ).status_code == 401
            ticket = await client.post(base + "/stream-ticket", headers=headers)
            assert ticket.status_code == 200 and ticket.headers["cache-control"] == "no-store"
            cookie = ticket.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            assert f"Path=/api/v1/runs/{run_id}/stream" in cookie
            assert ("Secure" in cookie) == secure
            token = ticket.cookies.get(STREAM_COOKIE_NAME)
            stream = await client.get(
                base + "/stream",
                headers={
                    "Cookie": f"{STREAM_COOKIE_NAME}={token}",
                },
            )
            assert stream.status_code == 200
            assert sse_events(stream.text)[0]["type"] == "RUN_STARTED"
            assert sse_events(stream.text)[-1]["type"] == "RUN_FINISHED"
            second = await completed_run(sessions)
            assert (
                await client.get(
                    f"/api/v1/runs/{second}/stream",
                    headers={
                        "Cookie": f"{STREAM_COOKIE_NAME}={token}",
                    },
                )
            ).status_code == 401
            assert (
                await client.get(
                    base,
                    headers={
                        "Cookie": f"{STREAM_COOKIE_NAME}={token}",
                    },
                )
            ).status_code == 401
            messages = (await client.get(base + "/messages", headers=headers)).json()["data"]
            assert [item["content"] for item in messages] == ["公开问题", "公开回答"]
            thread = await client.get("/api/v1/agents/1/ag-ui/threads/session", headers=headers)
            assert thread.headers["cache-control"] == "no-store"
            assert "messageDetails" in thread.json()["data"]

    asyncio.run(exercise())


def test_stream_cursor_validation_and_token_revocation(tmp_path: Path) -> None:
    """验证协议线游标及已建立观察连接的持续撤权检查。"""

    async def exercise():
        """在真实登录和隔离业务库中执行资源访问场景。"""
        async with environment(tmp_path) as (sessions, client, headers):
            run_id = await completed_run(sessions)
            url = f"/api/v1/runs/{run_id}/stream"
            snapshot = (await client.get(f"/api/v1/runs/{run_id}/ag-ui/snapshot", headers=headers)).json()["data"]
            latest = snapshot["latest_sequence"]
            for event_id in ("bad", "another-run:1", f"{run_id}:-1", f"{run_id}:abc", f"{run_id}:" + "9" * 30):
                assert (
                    await client.get(
                        url,
                        headers={
                            **headers,
                            "Last-Event-ID": event_id,
                        },
                    )
                ).status_code == 400
            assert (
                await client.get(
                    url,
                    params={
                        "after": latest + 1,
                    },
                    headers=headers,
                )
            ).status_code == 400
            assert (
                await client.get(
                    url,
                    params={
                        "after": -1,
                    },
                    headers=headers,
                )
            ).status_code == 400
            tail = await client.get(
                url,
                headers={
                    **headers,
                    "Last-Event-ID": f"{run_id}:{latest - 1}",
                },
            )
            assert len(sse_events(tail.text)) == 1
            assert f"id: {run_id}:{latest}" in tail.text
            claims = jwt.decode(
                headers["Authorization"].split()[1],
                options={
                    "verify_signature": False,
                },
            )
            async with sessions() as db:
                state = dict(auth_session_id=claims["sid"], auth_token_version=claims["ver"], auth_token_expires_at=0)
                expired = SimpleNamespace(state=SimpleNamespace(**state), is_disconnected=AsyncMock(return_value=False))
                stream = await stream_events(run_id, expired, after=0, last_event_id=None, actor=SimpleNamespace(id=1), db=db)
                with pytest.raises(StopAsyncIteration):
                    await anext(stream.body_iterator)
                state["auth_token_expires_at"] = claims["exp"]
                live = SimpleNamespace(state=SimpleNamespace(**state), is_disconnected=AsyncMock(return_value=False))
                stream = await stream_events(run_id, live, after=0, last_event_id=None, actor=SimpleNamespace(id=1), db=db)
                assert f"id: {run_id}:1" in await anext(stream.body_iterator)
                async with sessions.begin() as changed:
                    await changed.execute(update(Admin).where(Admin.id == 1).values(token_version=claims["ver"] + 1))
                # Authorization is checked before each source polling batch. Finish the already-loaded batch first.
                async for _ in stream.body_iterator:
                    pass
                assert (await client.get(url, headers=headers)).status_code == 401

    asyncio.run(exercise())
