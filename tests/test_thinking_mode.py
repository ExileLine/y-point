# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 01:18:04
# @Author  : yangyuexiong
# @File    : test_thinking_mode.py

"""验证思考偏好的 API 契约、模型兼容、幂等快照及只读失败恢复边界。"""

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.custom_exception import CustomException
from app.core.enums import ModelThinkingMode, ProviderKind, SessionModelReason
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.skill import RunSkill
from app.services.model_adapter import thinking_request_options
from app.services.run_executor import _can_rewind_failed_run
from app.services.session_models import model_issue


@pytest.mark.parametrize("kind", [kind for kind in ProviderKind if kind != ProviderKind.OPENAI_COMPATIBLE])
def test_unsupported_thinking_protocol_is_rejected(kind: ProviderKind) -> None:
    """不向未实现本次扩展协议的供应商静默发送或忽略思考开关。"""
    assert thinking_request_options(kind, ModelThinkingMode.AUTO) == {}
    for mode in (ModelThinkingMode.ENABLED, ModelThinkingMode.DISABLED):
        with pytest.raises(CustomException) as error:
            thinking_request_options(kind, mode)
        assert error.value.status_code == 409


def test_thinking_mode_api_snapshot_and_compatibility(tmp_path: Path) -> None:
    """通过正式路由验证创建、修改、模型选择、快照与幂等，不调用真实模型。"""

    async def exercise() -> None:
        """使用隔离库复验客户端配置到排队任务的完整写入契约。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'thinking.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def database():
            """向 API 提供独立测试事务。"""
            async with sessions() as db:
                yield db

        async def current_admin() -> Admin:
            """复用已认证测试身份，避免依赖真实令牌与登录状态。"""
            return Admin(id=1, role="admin", status=1, is_deleted=0)

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="thinking-user", password="not-used", role="admin"))
                for identifier, kind in ((1, "openai_compatible"), (2, "ollama")):
                    db.add(
                        ProviderConnection(
                            id=identifier,
                            name=kind,
                            name_key=kind,
                            kind=kind,
                            base_url="https://example.invalid/v1",
                            creator_id=1,
                        )
                    )
                    db.add(
                        ModelCatalog(
                            id=identifier,
                            provider_id=identifier,
                            external_id=f"model-{identifier}",
                            display_name=f"模型 {identifier}",
                            verified_capabilities=["text", "tools"],
                            context_window=32768,
                            creator_id=1,
                        )
                    )
            app.dependency_overrides[get_db_session] = database
            app.dependency_overrides[get_current_admin] = current_admin
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                denied = await client.post(
                    "/api/v1/agents",
                    json={
                        "name": "不支持的开关",
                        "default_model_id": 2,
                        "thinking_mode": "disabled",
                    },
                )
                assert denied.status_code == 409, denied.text
                created = await client.post(
                    "/api/v1/agents",
                    json={
                        "name": "兼容审查助手",
                        "default_model_id": 1,
                        "thinking_mode": "disabled",
                    },
                )
                assert created.status_code == 201, created.text
                agent = created.json()["data"]
                assert agent["thinking_mode"] == "disabled" and agent["version"] == 1
                agent_id = agent["id"]
                assert (
                    await client.put(
                        f"/api/v1/agents/{agent_id}",
                        json={
                            "expected_version": 1,
                            "thinking_mode": None,
                        },
                    )
                ).status_code == 400
                assert (
                    await client.put(
                        f"/api/v1/agents/{agent_id}",
                        json={
                            "expected_version": 1,
                            "default_model_id": 2,
                        },
                    )
                ).status_code == 409
                session = await client.post(f"/api/v1/agents/{agent_id}/sessions", json={})
                assert session.status_code == 201, session.text
                base = f"/api/v1/agents/{agent_id}/sessions/{session.json()['data']['id']}"
                options = await client.get(f"{base}/models")
                assert options.status_code == 200, options.text
                async with sessions() as db:
                    issue = model_issue(
                        await db.get(ModelCatalog, 2), await db.get(ProviderConnection, 2), await db.get(AgentConfig, agent_id), False
                    )
                    assert issue is not None and issue[0] == SessionModelReason.THINKING
                submitted = await client.post(
                    f"{base}/runs",
                    json={
                        "input_text": "请开始审核",
                        "request_key": "thinking-snapshot",
                    },
                )
                assert submitted.status_code == 201, submitted.text
                run = submitted.json()["data"]
                assert run["agent_thinking_mode"] == "disabled"
                changed = await client.put(
                    f"/api/v1/agents/{agent_id}",
                    json={
                        "expected_version": 1,
                        "thinking_mode": "auto",
                    },
                )
                assert changed.status_code == 200, changed.text
                replay = await client.post(
                    f"{base}/runs",
                    json={
                        "input_text": "请开始审核",
                        "request_key": "thinking-snapshot",
                    },
                )
                assert replay.status_code == 201, replay.text
                assert replay.json()["data"]["id"] == run["id"]
                assert replay.json()["data"]["agent_thinking_mode"] == "disabled"
                independent = await client.post(
                    "/api/v1/runs",
                    json={
                        "model_id": 1,
                        "input_text": "测试独立运行",
                        "request_key": "independent-thinking",
                        "thinking_mode": "disabled",
                    },
                )
                assert independent.status_code == 201, independent.text
                assert independent.json()["data"]["agent_thinking_mode"] == "disabled"
                conflict = await client.post(
                    "/api/v1/runs",
                    json={
                        "model_id": 1,
                        "input_text": "测试独立运行",
                        "request_key": "independent-thinking",
                        "thinking_mode": "auto",
                    },
                )
                assert conflict.status_code == 409, conflict.text
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            app.dependency_overrides.pop(get_current_admin, None)
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "tool_name,has_skill,allow",
    [
        ("list_session_artifacts", False, True),
        ("skill:review@v1", True, True),
        ("skill:review@v1", False, False),
        ("create_document_artifact", True, False),
        ("execute_script", True, False),
        ("unknown_tool", True, False),
    ],
)
def test_rewind_readonly_allowlist(tmp_path: Path, tool_name: str, has_skill: bool, allow: bool) -> None:
    """仅允许有完整计数证据的只读调用，生成、脚本和未知工具不能自动回退。"""

    async def exercise() -> None:
        """持久化工具事件并调用生产恢复判定，不伪造运行时返回值。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rewind.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = AgentRun(id="failed-run", owner_id=1, thread_id="failed-thread", state="failed", tool_call_count=1)
                db.add(run)
                if has_skill:
                    db.add(RunSkill(run_id=run.id, skill_id=1, skill_version=1))
                for sequence, event_type in ((1, "tool_started"), (2, "tool_completed")):
                    db.add(
                        AgentRunEvent(
                            run_id=run.id,
                            sequence=sequence,
                            event_type=event_type,
                            state="running",
                            tool_call_id="read-call",
                            tool_name=tool_name,
                        )
                    )
                await db.flush()
                assert await _can_rewind_failed_run(db, run) is allow
                run.tool_call_count = 2
                assert await _can_rewind_failed_run(db, run) is False
        finally:
            await engine.dispose()

    asyncio.run(exercise())
