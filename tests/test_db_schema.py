"""验证数据库迁移结构与启动时的缺失迁移检查。"""

import asyncio
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import artifacts as artifact_api
from app.core.config import get_config
from app.core.password import hash_password
from app.db.session import check_mapped_schema
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.artifact import Artifact
from app.models.base import Base, shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_manager import (
    RunConflictError, claim_run, complete_run, create_run, get_owned_messages, replay_events,
)


def test_startup_rejects_missing_artifact_column(tmp_path) -> None:
    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old-schema.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text("ALTER TABLE yp_artifact DROP COLUMN source_artifact_id"))
                with pytest.raises(RuntimeError, match="yp_artifact.source_artifact_id"):
                    await connection.run_sync(check_mapped_schema)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def _exercise_isolated_migrations(
    backend: str, base_url: URL, admin_database: str | None, database_env_key: str,
    private_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = f"yp_m3_schema_{uuid4().hex[:12]}"
    quote = "`" if backend == "mysql" else '"'
    identifier = f"{quote}{name}{quote}"
    create_sql = f"CREATE DATABASE {identifier}"
    if backend == "mysql":
        create_sql += " CHARACTER SET utf8mb4"
    admin_engine = create_async_engine(base_url.set(database=admin_database), isolation_level="AUTOCOMMIT")
    created = False
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(create_sql))
        created = True
        env = os.environ.copy()
        env.update({"DB_BACKEND": backend, database_env_key: name})
        root = Path(__file__).resolve().parents[1]
        for args in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head"), ("check",)):
            subprocess.run(
                [sys.executable, "-m", "alembic", *args], cwd=root, env=env,
                check=True, capture_output=True, text=True,
            )
        engine = create_async_engine(base_url.set(database=name))
        try:
            async with engine.connect() as connection:
                def verify(sync_connection) -> None:
                    schema = inspect(sync_connection)
                    assert {"alembic_version", *Base.metadata.tables} <= set(schema.get_table_names())
                    for table in Base.metadata.sorted_tables:
                        columns = {column["name"] for column in schema.get_columns(table.name)}
                        assert {column.name for column in table.columns} == columns
                        assert not schema.get_foreign_keys(table.name)

                await connection.run_sync(verify)
                await connection.run_sync(check_mapped_schema)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            artifact_id = str(uuid4())
            async with sessions.begin() as db:
                owner = Admin(
                    username=f"m3-{uuid4().hex}", password=hash_password("StrongPassword123"), role="user",
                )
                other = Admin(
                    username=f"m3-{uuid4().hex}", password=hash_password("StrongPassword123"), role="user",
                )
                db.add_all([owner, other])
                await db.flush()
                provider = ProviderConnection(
                    name="m3-test", name_key="m3-test", kind="ollama", base_url="http://localhost:11434/v1",
                    timeout_seconds=15, creator_id=owner.id, status=1, is_deleted=0,
                )
                db.add(provider)
                await db.flush()
                model = ModelCatalog(
                    provider_id=provider.id, external_id="m3-test", display_name="M3 test",
                    capabilities=["text"], verified_capabilities=[], capabilities_verified=False,
                    is_default=False, creator_id=owner.id, status=1, is_deleted=0,
                )
                db.add(model)
                await db.flush()
                db.add(Artifact(
                    id=artifact_id, owner_id=owner.id, filename="sample.txt", media_type="text/plain",
                    byte_size=3, sha256="0" * 64, storage_key=uuid4().hex,
                    source="upload", state="ready", status=1, is_deleted=0,
                ))
            async with sessions() as db:
                artifact = await db.scalar(select(Artifact).where(Artifact.id == artifact_id))
                assert artifact is not None and artifact.owner_id == owner.id
                assert artifact.filename == "sample.txt" and artifact.byte_size == 3
            async with sessions.begin() as db:
                run = await create_run(db, owner.id, request_key="m3-db-claim", input_text="test")

            async def try_claim() -> str:
                async with sessions.begin() as db:
                    try:
                        await claim_run(db, run.id)
                        return "claimed"
                    except RunConflictError:
                        return "conflict"

            assert sorted(await asyncio.gather(try_claim(), try_claim())) == ["claimed", "conflict"]
            async with sessions.begin() as db:
                claimed = await db.get(AgentRun, run.id)
                assert claimed is not None and claimed.state == "running" and claimed.lease_token
                assert claimed.lease_expires_at is not None and claimed.lease_expires_at.tzinfo is None
                remaining = claimed.lease_expires_at - shanghai_now(naive=True)
                assert timedelta(seconds=0) < remaining <= timedelta(seconds=32)
                completed = await complete_run(db, run.id, claimed.lease_token, final_text="done")
                assert completed.state == "completed"
            async with sessions() as db:
                assert [event.sequence for event in await replay_events(db, run.id, owner.id)] == [1, 2, 3]
                assert [message.content for message in await get_owned_messages(db, run.id, owner.id)] == [
                    "test", "done",
                ]

            async def test_session():
                async with sessions() as db:
                    yield db

            class StaticModel(FakeMessagesListChatModel):
                def bind_tools(self, tools, *, tool_choice=None, **kwargs):
                    return self

            async def resolve_model(db, run):
                return StaticModel(responses=[AIMessage(content="database reply")])

            artifact_root = private_root / "artifacts"
            artifact_root.mkdir(mode=0o700)
            with monkeypatch.context() as patcher:
                patcher.setattr(artifact_api, "artifact_root", lambda: artifact_root)
                app.dependency_overrides[get_db_session] = test_session
                try:
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://test",
                    ) as client:
                        async def login(username: str) -> dict[str, str]:
                            response = await client.post(
                                "/api/v1/auth/login",
                                json={"identifier": username, "password": "StrongPassword123"},
                            )
                            assert response.status_code == 200, response.text
                            return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}

                        owned = await login(owner.username)
                        outsider = await login(other.username)
                        uploaded = await client.post(
                            "/api/v1/artifacts/uploads", headers=owned,
                            files={"files": ("sample.txt", b"isolated database", "text/plain")},
                        )
                        assert uploaded.status_code == 201, uploaded.text
                        file_id = uploaded.json()["data"][0]["id"]
                        assert (await client.get(f"/api/v1/artifacts/{file_id}", headers=outsider)).status_code == 404
                        downloaded = await client.get(f"/api/v1/artifacts/{file_id}/download", headers=owned)
                        assert downloaded.status_code == 200 and downloaded.content == b"isolated database"
                        created = await client.post(
                            "/api/v1/runs", headers=owned,
                            json={"model_id": model.id, "input_text": "database question", "request_key": "http-run"},
                        )
                        assert created.status_code == 201, created.text
                        run_id = created.json()["data"]["id"]
                        duplicate = await client.post(
                            "/api/v1/runs", headers=owned,
                            json={"model_id": model.id, "input_text": "database question", "request_key": "http-run"},
                        )
                        assert duplicate.status_code == 201 and duplicate.json()["data"]["id"] == run_id
                        assert (await client.get(f"/api/v1/runs/{run_id}", headers=outsider)).status_code == 404
                        result = await dispatch_next_run(
                            sessions, private_root / "checkpoints.sqlite", resolve_model=resolve_model,
                            lease_seconds=2, poll_seconds=0.1,
                        )
                        assert result is not None and result.id == run_id and result.state == "completed"
                        messages = await client.get(f"/api/v1/runs/{run_id}/messages", headers=owned)
                        assert messages.status_code == 200, messages.text
                        assert [item["content"] for item in messages.json()["data"]] == [
                            "database question", "database reply",
                        ]
                        events = await client.get(f"/api/v1/runs/{run_id}/events?after=0", headers=owned)
                        assert events.status_code == 200, events.text
                        assert [item["state"] for item in events.json()["data"] if item["event_type"] == "state"] == [
                            "queued", "running", "completed",
                        ]
                        assert (await client.get(
                            f"/api/v1/runs/{run_id}/events?after=0", headers=outsider,
                        )).status_code == 404
                        removed = await client.delete(f"/api/v1/artifacts/{file_id}", headers=owned)
                        assert removed.status_code == 200, removed.text
                        assert (await client.get(
                            f"/api/v1/artifacts/{file_id}/download", headers=owned,
                        )).status_code == 404
                finally:
                    app.dependency_overrides.pop(get_db_session, None)
        finally:
            await engine.dispose()
    finally:
        if created:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f"DROP DATABASE {identifier}"))
        await admin_engine.dispose()


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_MYSQL_M3") != "1", reason="需显式启用隔离 MySQL 迁移验收")
def test_mysql_m3_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在独立 MySQL 库验证迁移、并发领取及基础 HTTP/worker 链路。"""
    asyncio.run(_exercise_isolated_migrations(
        "mysql", make_url(get_config().mysql_async_url), None, "MYSQL_DATABASE", tmp_path, monkeypatch,
    ))


@pytest.mark.skipif(
    os.environ.get("YPOINT_TEST_POSTGRES_M3") != "1", reason="需显式启用隔离 PostgreSQL 迁移验收",
)
def test_postgres_m3_migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """在独立 PostgreSQL 库验证迁移、并发领取及基础 HTTP/worker 链路。"""
    asyncio.run(_exercise_isolated_migrations(
        "postgres", make_url(get_config().pg_async_url), "postgres", "POSTGRESQL_DATABASE", tmp_path, monkeypatch,
    ))
