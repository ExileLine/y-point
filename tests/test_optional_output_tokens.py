# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 19:59:52
# @Author  : yangyuexiong
# @File    : test_optional_output_tokens.py

"""验证输出额度可空、运行快照和 SDK 实际请求的一致性。"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import ModelCapability, ProviderKind, SessionModelReason
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.agent import AgentCreateIn
from app.services import model_adapter
from app.services.model_adapter import build_chat_model
from app.services.run_dispatcher import resolve_stored_model
from app.services.run_manager import RunConflictError, create_run
from app.services.runtime_adapter import CONTEXT_OVERHEAD_TOKENS, RuntimeRunConfig
from app.services.session_models import model_issue


def test_optional_output_agent_http_and_snapshot(tmp_path: Path) -> None:
    """通过正式路由验证创建省略、显式空值、修改清空、快照及幂等提交。"""

    async def exercise() -> None:
        """隔离数据库和认证依赖，保留真实接口校验与持久化逻辑。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'optional-output.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="optional-output-owner", password="unused", status=1, is_deleted=0)

        async def session_override():
            """为每个 HTTP 请求提供独立事务会话。"""
            async with sessions() as db:
                yield db

        previous_overrides = app.dependency_overrides.copy()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(actor)
                db.add(
                    ProviderConnection(
                        id=1,
                        name="本地兼容供应商",
                        name_key="optional-output",
                        kind=ProviderKind.OPENAI_COMPATIBLE,
                        base_url="http://127.0.0.1:9/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="local-model",
                        display_name="本地测试模型",
                        verified_capabilities=[ModelCapability.TEXT],
                        context_window=65536,
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
            app.dependency_overrides[get_db_session] = session_override
            app.dependency_overrides[get_current_admin] = lambda: actor
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                default = await client.post(
                    "/api/v1/agents",
                    json={
                        "name": "保留创建默认值",
                        "default_model_id": 1,
                    },
                )
                assert default.status_code == 201, default.text
                assert default.json()["data"]["max_output_tokens"] == get_config().AGENT_DEFAULT_MAX_OUTPUT_TOKENS
                created = await client.post(
                    "/api/v1/agents",
                    json={
                        "name": "供应商决定额度",
                        "default_model_id": 1,
                        "max_output_tokens": None,
                    },
                )
                assert created.status_code == 201, created.text
                agent = created.json()["data"]
                assert agent["max_output_tokens"] is None
                agent_url = f"/api/v1/agents/{agent['id']}"
                for change, expected in (
                    (
                        {
                            "description": "保留空值",
                        },
                        None,
                    ),
                    (
                        {
                            "max_output_tokens": 2048,
                        },
                        2048,
                    ),
                    (
                        {
                            "max_output_tokens": None,
                        },
                        None,
                    ),
                ):
                    updated = await client.put(
                        agent_url,
                        json={
                            "expected_version": agent["version"],
                            **change,
                        },
                    )
                    assert updated.status_code == 200, updated.text
                    agent = updated.json()["data"]
                    assert agent["max_output_tokens"] == expected
                session = await client.post(f"{agent_url}/sessions", json={})
                assert session.status_code == 201, session.text
                session_id = session.json()["data"]["id"]
                payload = {
                    "input_text": "检查可空额度",
                    "request_key": str(uuid4()),
                    "expected_session_version": 1,
                }
                submitted = await client.post(f"{agent_url}/sessions/{session_id}/runs", json=payload)
                assert submitted.status_code == 201, submitted.text
                run_id = submitted.json()["data"]["id"]
                retry = await client.post(f"{agent_url}/sessions/{session_id}/runs", json=payload)
                assert retry.status_code == 201 and retry.json()["data"]["id"] == run_id
                async with sessions() as db:
                    saved = await db.get(AgentConfig, agent["id"])
                    run = await db.get(AgentRun, run_id)
                    assert saved.max_output_tokens is None and run.agent_max_output_tokens is None
                    resolved = await resolve_stored_model(db, run)
                    assert resolved.max_tokens is None
                    await resolved.async_client._client.close()
                blocked = await client.post(
                    "/api/v1/agents",
                    json={
                        "name": "无效额度",
                        "default_model_id": 1,
                        "max_output_tokens": 0,
                    },
                )
                assert blocked.status_code == 400
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)
            await engine.dispose()

    asyncio.run(exercise())


def test_output_snapshot_distinguishes_omission_and_null(tmp_path: Path) -> None:
    """内部提交省略额度沿用平台默认值，显式空值持久化且参与幂等冲突判断。"""

    async def exercise() -> None:
        """独立事务核对运行快照与原有默认值兼容性。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'output-snapshot.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                default = await create_run(db, 1, input_text="默认额度")
                assert default.agent_max_output_tokens == get_config().AGENT_DEFAULT_MAX_OUTPUT_TOKENS
                uncapped = await create_run(db, 1, input_text="供应商额度", agent_max_output_tokens=None, request_key="optional-null")
                assert uncapped.agent_max_output_tokens is None
                again = await create_run(db, 1, input_text="供应商额度", agent_max_output_tokens=None, request_key="optional-null")
                assert again.id == uncapped.id
                with pytest.raises(RunConflictError):
                    await create_run(db, 1, input_text="供应商额度", request_key="optional-null")
                with pytest.raises(ValueError, match="快照"):
                    await create_run(
                        db,
                        1,
                        session_id=str(uuid4()),
                        agent_id=1,
                        thread_id=str(uuid4()),
                        agent_config_version=1,
                        agent_system_prompt="",
                        agent_temperature=Decimal("1"),
                    )
            async with sessions() as db:
                saved = await db.get(AgentRun, uncapped.id)
                assert saved.agent_max_output_tokens is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", [ProviderKind.OPENAI_COMPATIBLE, ProviderKind.OPENAI, ProviderKind.OLLAMA, ProviderKind.AZURE_OPENAI])
@pytest.mark.parametrize("limit", [None, 256])
def test_optional_output_wire_payload(monkeypatch: pytest.MonkeyPatch, kind: ProviderKind, limit: int | None) -> None:
    """保留真实 LangChain 与 SDK，验证空值不产生任何输出额度请求参数。"""

    async def exercise() -> None:
        """仅替换 HTTP 传输并核对最终序列化的请求体。"""
        monkeypatch.setattr(model_adapter, "decrypt_credentials", lambda _: ("test-key", {}))
        provider = ProviderConnection(
            id=1,
            kind=kind,
            base_url="https://local.example/v1",
            api_version="2024-10-21",
            timeout_seconds=5,
            status=1,
            is_deleted=0,
        )
        catalog = ModelCatalog(id=1, provider_id=1, external_id="compatible-model", status=1, is_deleted=0)
        requests = []

        def respond(request: httpx.Request) -> httpx.Response:
            """以正常完成响应检查 HTTP 请求体中的空值是否真正被省略。"""
            payload = json.loads(request.content)
            requests.append(payload)
            for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                if limit is None:
                    assert key not in payload
            if limit is not None:
                assert payload.get("max_completion_tokens", payload.get("max_tokens")) == limit
            return httpx.Response(
                200,
                json={
                    "id": "local-output-test",
                    "model": "compatible-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "OK",
                            },
                        },
                    ],
                    "usage": {
                        "prompt_tokens": 8,
                        "completion_tokens": 2,
                        "total_tokens": 10,
                    },
                },
            )

        constructor_name = "AzureChatOpenAI" if kind == ProviderKind.AZURE_OPENAI else "ChatOpenAI"
        constructor = getattr(model_adapter, constructor_name)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:

            def configured_client(**kwargs):
                """保留真实 SDK 构造，只注入隔离 HTTP 客户端。"""
                return constructor(http_async_client=client, **kwargs)

            monkeypatch.setattr(model_adapter, constructor_name, configured_client)
            model = build_chat_model(catalog, provider, max_output_tokens=limit)
            result = await model.ainvoke("请回复 OK")
            assert result.text == "OK" and len(requests) == 1

    asyncio.run(exercise())


def test_provider_required_limit_and_input_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """必须指定额度的供应商拒绝空值，输入预算不暗中使用平台输出默认值。"""
    monkeypatch.setattr(model_adapter, "decrypt_credentials", lambda _: ("test-key", {}))
    provider = ProviderConnection(id=1, kind=ProviderKind.ANTHROPIC, base_url="https://local.example/v1", status=1, is_deleted=0)
    catalog = ModelCatalog(id=1, provider_id=1, external_id="claude-test", verified_capabilities=[ModelCapability.TEXT], status=1, is_deleted=0)
    with pytest.raises(CustomException, match="Anthropic"):
        build_chat_model(catalog, provider, max_output_tokens=None)
    agent = AgentConfig(max_output_tokens=None)
    issue = model_issue(catalog, provider, agent, False)
    assert issue[0] == SessionModelReason.OUTPUT and "Anthropic" in issue[1]
    configured = build_chat_model(catalog, provider, max_output_tokens=256)
    assert configured.max_tokens == 256
    probe = build_chat_model(catalog, provider)
    assert probe.max_tokens == 1024
    config = RuntimeRunConfig(run_id="run", owner_id=1, thread_id="thread", model_id=1, tool_names=(), context_window=65536, max_output_tokens=None)
    assert config.input_budget() == 65536 - CONTEXT_OVERHEAD_TOKENS
    assert AgentCreateIn(name="保留默认", default_model_id=1).max_output_tokens == get_config().AGENT_DEFAULT_MAX_OUTPUT_TOKENS


def test_optional_output_migration(tmp_path: Path) -> None:
    """验证已有整数额度不被改写，空值可写入，回退及重升后结构一致。"""
    database = tmp_path / "output-migration.db"
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        },
    )
    root = Path(__file__).resolve().parents[1]

    def migrate(action: str, revision: str) -> None:
        """仅迁移独立测试库，不访问本地真实业务数据。"""
        subprocess.run([sys.executable, "-m", "alembic", action, revision], cwd=root, env=env, capture_output=True, check=True, text=True)

    migrate("upgrade", "048")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO yp_agent_config
            (id, owner_id, name, system_prompt, default_model_id, temperature, max_output_tokens, model_timeout_seconds, tool_timeout_seconds,
             version, enabled, status, is_deleted, create_time, update_time, create_timestamp, update_timestamp)
            VALUES (1, 1, '历史智能体', '', 1, 1, 16384, 120, 120, 1, 1, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, 0)""",
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT max_output_tokens FROM yp_agent_config WHERE id=1").fetchone() == (16384,)
        connection.execute("UPDATE yp_agent_config SET max_output_tokens=NULL WHERE id=1")
    migrate("downgrade", "048")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT max_output_tokens FROM yp_agent_config WHERE id=1").fetchone() == (4096,)
    migrate("upgrade", "head")
    subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=root, env=env, capture_output=True, check=True, text=True)
