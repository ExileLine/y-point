"""验证可空模型时限从 HTTP 配置、持久化快照到真实 Worker 的完整语义。"""

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import BaseConfig, get_config
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.run_executor import execute_text_run
from app.services.run_manager import RunConflictError, create_run
from tests.ag_ui_helpers import submit_agent_run


def test_optional_model_timeout_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置和空环境变量均不设置平台倒计时，显式正数仍校验部署上限。"""
    monkeypatch.delenv("AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", raising=False)
    assert BaseConfig(_env_file=None).AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS is None
    monkeypatch.setenv("AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", "")
    assert BaseConfig(_env_file=None).AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS is None
    assert BaseConfig(_env_file=None, AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS=60).AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS == 60
    for value in (0, -1):
        with pytest.raises(ValidationError):
            BaseConfig(_env_file=None, AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS=value)
    with pytest.raises(ValidationError, match="不能超过"):
        BaseConfig(_env_file=None, AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS=301, AGENT_MAX_MODEL_TIMEOUT_SECONDS=300)


def test_optional_model_timeout_http_and_run_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式清空覆盖非空部署默认值，省略更新保留原值，AG-UI 提交固定空值快照。"""
    monkeypatch.setattr(get_config(), "AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", 300)

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'optional-timeout.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="optional-timeout-owner", password="unused", status=1, is_deleted=0)

        async def session_override():
            async with sessions() as db:
                yield db

        previous = app.dependency_overrides.copy()
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all([
                    actor,
                    ProviderConnection(id=1, name="测试连接", name_key="timeout", kind="openai_compatible", base_url="http://127.0.0.1:9/v1", creator_id=1),
                    ModelCatalog(id=1, provider_id=1, external_id="test-model", display_name="测试模型", verified_capabilities=["text"], creator_id=1),
                ])
            app.dependency_overrides[get_db_session] = session_override
            app.dependency_overrides[get_current_admin] = lambda: actor
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for supplied, expected in (({}, 300), ({"model_timeout_seconds": None}, None)):
                    response = await client.post("/api/v1/agents", json={"name": "时限测试", "default_model_id": 1, **supplied})
                    assert response.status_code == 201, response.text
                    agent = response.json()["data"]
                    assert agent["model_timeout_seconds"] == expected
                url = f"/api/v1/agents/{agent['id']}"
                for changes, expected in (({"model_timeout_seconds": 60}, 60), ({"description": "保留原时限"}, 60), ({"model_timeout_seconds": None}, None)):
                    response = await client.put(url, json={"expected_version": agent["version"], **changes})
                    assert response.status_code == 200, response.text
                    agent = response.json()["data"]
                    assert agent["model_timeout_seconds"] == expected
                for value in (0, -1, get_config().AGENT_MAX_MODEL_TIMEOUT_SECONDS + 1):
                    response = await client.put(url, json={"expected_version": agent["version"], "model_timeout_seconds": value})
                    assert response.status_code == 400, response.text
                response = await client.post(f"{url}/sessions", json={})
                assert response.status_code == 201, response.text
                session_id = response.json()["data"]["id"]
                response = await submit_agent_run(client, f"{url}/sessions/{session_id}/runs", json={"input_text": "验证空时限快照"})
                assert response.status_code == 201, response.text
                run_id = response.json()["data"]["id"]
            async with sessions() as db:
                assert (await db.get(AgentConfig, agent["id"])).model_timeout_seconds is None
                assert (await db.get(AgentRun, run_id)).model_timeout_seconds is None
            async with sessions.begin() as db:
                default_run = await create_run(db, 1, input_text="默认时限")
                assert default_run.model_timeout_seconds == 300
                empty_run = await create_run(db, 1, input_text="空时限", request_key="optional-timeout", model_timeout_seconds=None)
                assert empty_run.model_timeout_seconds is None
                replay = await create_run(db, 1, input_text="空时限", request_key="optional-timeout", model_timeout_seconds=None)
                assert replay.id == empty_run.id
                with pytest.raises(RunConflictError):
                    await create_run(db, 1, input_text="空时限", request_key="optional-timeout", model_timeout_seconds=60)
        finally:
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous)
            await engine.dispose()

    asyncio.run(exercise())


class TimeoutProbeModel(FakeMessagesListChatModel):
    """执行真实 DeepAgents 图，仅在模型边界模拟等待和上游超时。"""

    outcome: str

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.outcome == "platform_timeout":
            await asyncio.Event().wait()
        await asyncio.sleep(0.03)
        if self.outcome == "provider_timeout":
            raise httpx.ReadTimeout("private-upstream-timeout")
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("outcome", ["success", "platform_timeout", "provider_timeout"])
def test_worker_respects_nullable_timeout_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str) -> None:
    """空快照不被 Worker 当前默认值覆盖，显式平台时限和供应商超时仍各自生效。"""
    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="timeout-worker", password="unused"))
                run = await create_run(db, 1, input_text="测试等待", model_timeout_seconds=1 if outcome == "platform_timeout" else None)
            monkeypatch.setattr(get_config(), "AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", 0.001)
            result = await execute_text_run(sessions, run.id, TimeoutProbeModel(outcome=outcome, responses=[AIMessage(content="完成")]), tmp_path / "checkpoints.db", timeout_seconds=5)
            expected = {"success": ("completed", None), "platform_timeout": ("timed_out", "model_execution_timeout"), "provider_timeout": ("timed_out", "provider_timeout")}
            assert (result.state, result.failure_code) == expected[outcome]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_optional_model_timeout_migration(tmp_path: Path) -> None:
    """真实迁移保留历史非空配置，可写空值且支持回退后再升级。"""
    database = tmp_path / "migration.db"
    env = {**os.environ, "DB_BACKEND": "sqlite", "SQLITE_DATABASE": str(database)}
    root = Path(__file__).resolve().parents[1]

    def migrate(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, capture_output=True, text=True, check=True)

    migrate("upgrade", "059")
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO yp_agent_config (id,owner_id,name,system_prompt,default_model_id,temperature,max_output_tokens,model_timeout_seconds,tool_timeout_seconds,enabled,version,create_time,update_time,create_timestamp,update_timestamp,is_deleted,status) VALUES (1,1,'保留历史','',1,1,4096,60,30,1,1,'2026-11-19','2026-11-19',1,1,0,1)")
        db.execute("INSERT INTO yp_agent_run (id,owner_id,thread_id,state,version,event_seq,model_timeout_seconds,tool_timeout_seconds,create_time,update_time,create_timestamp,update_timestamp,is_deleted,status) VALUES ('run',1,'thread','completed',1,0,75,30,'2026-11-19','2026-11-19',1,1,0,1)")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as db:
        for name, expected in (("yp_agent_config", 60), ("yp_agent_run", 75)):
            assert db.execute(f"SELECT model_timeout_seconds FROM {name}").fetchone() == (expected,)
            column = next(row for row in db.execute(f"PRAGMA table_info({name})") if row[1] == "model_timeout_seconds")
            assert column[3] == 0 and column[4] is None
            db.execute(f"UPDATE {name} SET model_timeout_seconds=NULL")
    migrate("downgrade", "059")
    with sqlite3.connect(database) as db:
        for name in ("yp_agent_config", "yp_agent_run"):
            assert db.execute(f"SELECT model_timeout_seconds FROM {name}").fetchone() == (120,)
    migrate("upgrade", "head")
    migrate("check")
