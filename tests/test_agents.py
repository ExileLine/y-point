# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 11:00:31
# @Author  : yangyuexiong
# @File    : test_agents.py

"""验证私有 Agent 配置接口、所有权和迁移。"""

import asyncio
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_share import AgentShare
from app.models.audit_event import AuditEvent
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.base import shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection


def test_agent_config_migration(tmp_path: Path) -> None:
    """独立 SQLite 库验证新表迁移、回退及无外键结构。"""
    database = tmp_path / "agent-migration.db"
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        }
    )
    root = Path(__file__).resolve().parents[1]
    for args in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head"), ("check",)):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, check=True, capture_output=True, text=True)

    async def inspect_table() -> None:
        """确认迁移后的列和 ORM 声明一致。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        try:
            async with engine.connect() as connection:

                def inspect_schema(sync_connection) -> None:
                    """读取同步 SQLAlchemy Inspector 的表结构。"""
                    schema = inspect(sync_connection)
                    assert {column.name for column in AgentConfig.__table__.columns} == {
                        column["name"] for column in schema.get_columns("yp_agent_config")
                    }
                    assert schema.get_foreign_keys("yp_agent_config") == []
                    assert schema.get_foreign_keys("yp_agent_share") == []
                    assert {column.name for column in AgentShare.__table__.columns} == {
                        column["name"] for column in schema.get_columns("yp_agent_share")
                    }

                await connection.run_sync(inspect_schema)
        finally:
            await engine.dispose()

    asyncio.run(inspect_table())


def test_agent_limits_backfill(tmp_path: Path) -> None:
    """已有 Agent 升级到 033 后保留配置并获得默认超时。"""
    database = tmp_path / "agent-limits-backfill.db"
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        }
    )
    root = Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "032"], cwd=root, env=env, check=True, capture_output=True, text=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO yp_agent_config (
                id, owner_id, name, system_prompt, default_model_id, temperature, max_output_tokens,
                enabled, version, create_time, update_time, create_timestamp, update_timestamp, is_deleted, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (1, 9, "旧配置", "保留系统指令", 3, 0.7, 1024, 1, 4, "2026-10-26 12:00:00", "2026-10-26 12:00:00", 1, 1, 0, 1),
        )
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True, capture_output=True, text=True)
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            """SELECT name, version, model_timeout_seconds, tool_timeout_seconds,
                max_model_calls, max_tool_calls, max_total_tokens, max_estimated_cost
                FROM yp_agent_config WHERE id = 1"""
        ).fetchone()
    assert row == ("旧配置", 4, 120, 120, None, None, None, None)


def test_private_agent_crud(tmp_path: Path) -> None:
    """验证 Agent CRUD、模型门槛、用户隔离、版本保护和审计。"""

    async def exercise() -> None:
        """在独立业务库通过真实 HTTP 路由运行操作链。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'agents.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(
                            id=1, username="agent-alice", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0
                        ),
                        Admin(id=2, username="agent-bob", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                        Admin(
                            id=3, username="agent-admin", password=hash_password("StrongPassword123"), role="admin", status=1, is_deleted=0
                        ),
                        ProviderConnection(
                            id=1,
                            name="agent-provider",
                            name_key="agent-provider",
                            kind="ollama",
                            base_url="http://localhost:11434/v1",
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=1,
                            provider_id=1,
                            external_id="verified",
                            display_name="Verified",
                            capabilities=["text"],
                            verified_capabilities=["text"],
                            capabilities_verified=True,
                            context_window=8192,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=2,
                            provider_id=1,
                            external_id="unverified",
                            display_name="Unverified",
                            capabilities=["text"],
                            verified_capabilities=[],
                            capabilities_verified=False,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                    ]
                )

            async def test_session():
                """将接口会话切换到隔离数据库。"""
                async with sessions() as db:
                    yield db

            app.dependency_overrides[get_db_session] = test_session
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

                    async def login(username: str) -> dict[str, str]:
                        """取得测试用户的 Bearer 请求头。"""
                        response = await client.post(
                            "/api/v1/auth/login",
                            json={
                                "identifier": username,
                                "password": "StrongPassword123",
                            },
                        )
                        assert response.status_code == 200, response.text
                        return {
                            "Authorization": f"Bearer {response.json()['data']['access_token']}",
                        }

                    alice, bob = await login("agent-alice"), await login("agent-bob")
                    admin = await login("agent-admin")
                    payload = {
                        "name": " 需求助手 ",
                        "default_model_id": 1,
                        "system_prompt": "只回答需求问题",
                        "temperature": "0.70",
                        "model_timeout_seconds": 60,
                        "tool_timeout_seconds": 30,
                        "max_model_calls": 3,
                        "max_tool_calls": 5,
                        "max_total_tokens": 10000,
                    }
                    assert (await client.post("/api/v1/agents", json=payload)).status_code == 401
                    invalid = await client.post(
                        "/api/v1/agents",
                        headers=alice,
                        json={
                            **payload,
                            "default_model_id": 2,
                        },
                    )
                    assert invalid.status_code == 409
                    too_large = await client.post(
                        "/api/v1/agents",
                        headers=alice,
                        json={
                            **payload,
                            "max_output_tokens": 9000,
                        },
                    )
                    assert too_large.status_code == 409
                    crowded = await client.post(
                        "/api/v1/agents",
                        headers=alice,
                        json={
                            **payload,
                            "max_output_tokens": 7500,
                        },
                    )
                    assert crowded.status_code == 409
                    unpriced = await client.post(
                        "/api/v1/agents",
                        headers=alice,
                        json={
                            **payload,
                            "max_estimated_cost": "0.010000",
                        },
                    )
                    assert unpriced.status_code == 409
                    for field, invalid_value in (
                        ("model_timeout_seconds", 121),
                        ("tool_timeout_seconds", 0),
                        ("max_model_calls", 101),
                        ("max_tool_calls", 0),
                        ("max_total_tokens", 0),
                        ("max_estimated_cost", "0.0000001"),
                    ):
                        rejected = await client.post(
                            "/api/v1/agents",
                            headers=alice,
                            json={
                                **payload,
                                field: invalid_value,
                            },
                        )
                        assert rejected.status_code == 400, rejected.text
                    created = await client.post("/api/v1/agents", headers=alice, json=payload)
                    assert created.status_code == 201, created.text
                    agent = created.json()["data"]
                    assert agent["name"] == "需求助手" and agent["version"] == 1
                    assert agent["temperature"] == "0.70" and agent["enabled"] is True
                    assert agent["model_timeout_seconds"] == 60 and agent["tool_timeout_seconds"] == 30
                    assert agent["max_model_calls"] == 3 and agent["max_tool_calls"] == 5
                    assert agent["max_total_tokens"] == 10000 and agent["max_estimated_cost"] is None
                    agent_id = agent["id"]
                    assert (
                        await client.put(
                            "/api/v1/models/1",
                            headers=admin,
                            json={
                                "status": 0,
                            },
                        )
                    ).status_code == 409
                    assert (await client.delete("/api/v1/models/1", headers=admin)).status_code == 409
                    assert (
                        await client.put(
                            "/api/v1/providers/1",
                            headers=admin,
                            json={
                                "status": 0,
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.put(
                            "/api/v1/models/1",
                            headers=admin,
                            json={
                                "external_id": "replacement",
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.put(
                            "/api/v1/providers/1",
                            headers=admin,
                            json={
                                "base_url": "http://localhost:11435/v1",
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.put(
                            "/api/v1/models/1",
                            headers=admin,
                            json={
                                "display_name": "Renamed",
                            },
                        )
                    ).status_code == 200
                    assert (await client.get(f"/api/v1/agents/{agent_id}", headers=bob)).status_code == 404
                    assert (
                        await client.put(
                            f"/api/v1/agents/{agent_id}",
                            headers=bob,
                            json={
                                "expected_version": 1,
                                "name": "steal",
                            },
                        )
                    ).status_code == 404
                    assert (await client.delete(f"/api/v1/agents/{agent_id}", headers=bob)).status_code == 404
                    assert (await client.get("/api/v1/agents", headers=bob)).json()["data"]["total"] == 0
                    listed = (await client.get("/api/v1/agents?page=1&page_size=1", headers=alice)).json()["data"]
                    assert listed["total"] == 1 and listed["items"][0]["id"] == agent_id
                    updated = await client.put(
                        f"/api/v1/agents/{agent_id}",
                        headers=alice,
                        json={
                            "expected_version": 1,
                            "description": "新版描述",
                            "enabled": False,
                            "max_model_calls": None,
                            "max_total_tokens": None,
                        },
                    )
                    assert updated.status_code == 200, updated.text
                    assert updated.json()["data"]["version"] == 2
                    assert updated.json()["data"]["max_model_calls"] is None
                    assert updated.json()["data"]["max_total_tokens"] is None
                    assert (
                        await client.put(
                            f"/api/v1/agents/{agent_id}",
                            headers=alice,
                            json={
                                "expected_version": 2,
                                "tool_timeout_seconds": None,
                            },
                        )
                    ).status_code == 400
                    assert (
                        await client.put(
                            f"/api/v1/agents/{agent_id}",
                            headers=alice,
                            json={
                                "expected_version": 2,
                                "max_estimated_cost": "0.020000",
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.put(
                            f"/api/v1/agents/{agent_id}",
                            headers=alice,
                            json={
                                "expected_version": 1,
                                "name": "覆盖新版",
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.put(
                            f"/api/v1/agents/{agent_id}",
                            headers=alice,
                            json={
                                "expected_version": 2,
                                "default_model_id": 2,
                            },
                        )
                    ).status_code == 409
                    assert (
                        await client.put(
                            f"/api/v1/agents/{agent_id}",
                            headers=alice,
                            json={
                                "expected_version": 2,
                                "max_output_tokens": 9000,
                            },
                        )
                    ).status_code == 409
                    assert (await client.delete(f"/api/v1/agents/{agent_id}", headers=alice)).status_code == 200
                    assert (
                        await client.put(
                            "/api/v1/models/1",
                            headers=admin,
                            json={
                                "status": 0,
                            },
                        )
                    ).status_code == 200
                    assert (await client.delete("/api/v1/models/1", headers=admin)).status_code == 200
                    assert (await client.get(f"/api/v1/agents/{agent_id}", headers=alice)).status_code == 404
                    assert (await client.get("/api/v1/agents", headers=alice)).json()["data"]["total"] == 0
            finally:
                app.dependency_overrides.pop(get_db_session, None)
            async with sessions() as db:
                saved = await db.get(AgentConfig, agent_id)
                assert saved is not None and saved.is_deleted == 1 and saved.version == 3
                actions = (await db.scalars(select(AuditEvent.action).where(AuditEvent.target_id == agent_id))).all()
                assert {"agent.created", "agent.updated", "agent.deleted"} <= set(actions)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_named_user_agent_share(tmp_path: Path) -> None:
    """指定用户能创建自己的会话；撤权后历史入口关闭且排队运行取消。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：指定用户能创建自己的会话；撤权后历史入口关闭且排队运行取消。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'named-share.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def test_session():
            """为 HTTP 测试注入隔离数据库会话。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db_session] = test_session
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=user_id, username=name, password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0)
                        for user_id, name in ((1, "share-owner"), (2, "share-recipient"), (3, "share-stranger"))
                    ]
                )
                db.add(
                    ProviderConnection(
                        id=1,
                        name="share-provider",
                        name_key="share-provider",
                        kind="ollama",
                        base_url="http://localhost:11434/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    )
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="verified",
                        display_name="Verified",
                        capabilities=["text"],
                        verified_capabilities=["text"],
                        capabilities_verified=True,
                        context_window=8192,
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    )
                )

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

                async def login(name: str) -> dict[str, str]:
                    """通过正式登录接口获取测试用户的访问令牌。"""
                    response = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "identifier": name,
                            "password": "StrongPassword123",
                        },
                    )
                    assert response.status_code == 200, response.text
                    return {
                        "Authorization": f"Bearer {response.json()['data']['access_token']}",
                    }

                owner, recipient, stranger = await login("share-owner"), await login("share-recipient"), await login("share-stranger")
                assert (await client.get("/api/v1/agents/share-targets", params={"query": "sh"})).status_code == 401
                targets = await client.get("/api/v1/agents/share-targets", headers=owner, params={"query": "share-rec"})
                assert targets.status_code == 200
                assert targets.json()["data"] == [{"user_id": 2, "username": "share-recipient"}]
                recipient_targets = await client.get("/api/v1/agents/share-targets", headers=recipient, params={"query": "share-rec"})
                assert recipient_targets.json()["data"] == []
                created = await client.post(
                    "/api/v1/agents",
                    headers=owner,
                    json={
                        "name": "Private prompt",
                        "default_model_id": 1,
                        "system_prompt": "owner-only-secret",
                    },
                )
                assert created.status_code == 201, created.text
                agent_id = created.json()["data"]["id"]
                base = f"/api/v1/agents/{agent_id}"
                assert (
                    await client.post(
                        f"{base}/sessions",
                        headers=recipient,
                        json={
                            "title": "mine",
                        },
                    )
                ).status_code == 404
                assert (
                    await client.post(
                        f"{base}/shares",
                        headers=recipient,
                        json={
                            "user_id": 2,
                        },
                    )
                ).status_code == 404
                assert (
                    await client.post(
                        f"{base}/shares",
                        headers=owner,
                        json={
                            "user_id": 1,
                        },
                    )
                ).status_code == 404
                assert (
                    await client.post(
                        f"{base}/shares",
                        headers=owner,
                        json={
                            "user_id": 999,
                        },
                    )
                ).status_code == 404
                granted = await client.post(
                    f"{base}/shares",
                    headers=owner,
                    json={
                        "user_id": 2,
                    },
                )
                assert granted.status_code == 200, granted.text
                assert (
                    await client.post(
                        f"{base}/shares",
                        headers=owner,
                        json={
                            "user_id": 2,
                        },
                    )
                ).status_code == 200
                assert (await client.get(f"{base}/shares", headers=owner)).json()["data"] == [
                    {
                        "user_id": 2,
                        "username": "share-recipient",
                    }
                ]
                shared = (await client.get("/api/v1/agents/shared", headers=recipient)).json()["data"]
                assert len(shared) == 1 and shared[0]["id"] == agent_id
                assert "system_prompt" not in shared[0]
                assert (await client.get(f"/api/v1/agents/shared/{agent_id}", headers=recipient)).json()["data"]["id"] == agent_id
                assert (await client.get(f"/api/v1/agents/shared/{agent_id}", headers=stranger)).status_code == 404
                assert (await client.get(base, headers=recipient)).status_code == 404
                assert (await client.get(f"{base}/shares", headers=recipient)).status_code == 404
                assert (await client.get("/api/v1/agents/shared", headers=stranger)).json()["data"] == []

                owner_session = await client.post(
                    f"{base}/sessions",
                    headers=owner,
                    json={
                        "title": "owner history",
                    },
                )
                assert owner_session.status_code == 201
                owner_session_id = owner_session.json()["data"]["id"]
                recipient_session = await client.post(
                    f"{base}/sessions",
                    headers=recipient,
                    json={
                        "title": "recipient history",
                    },
                )
                assert recipient_session.status_code == 201, recipient_session.text
                recipient_session_id = recipient_session.json()["data"]["id"]
                assert (await client.get(f"{base}/sessions", headers=recipient)).json()["data"]["total"] == 1
                assert (await client.get(f"{base}/sessions/{owner_session_id}/messages", headers=recipient)).status_code == 404
                assert (await client.get(f"{base}/sessions/{recipient_session_id}/messages", headers=owner)).status_code == 404
                assert (await client.get(f"{base}/sessions/{recipient_session_id}", headers=stranger)).status_code == 404
                foreign_artifact_id = str(uuid4())
                recipient_artifact_id = str(uuid4())
                async with sessions.begin() as db:
                    db.add_all(
                        [
                            Artifact(
                                id=artifact_id,
                                owner_id=owner_id,
                                filename=filename,
                                media_type="text/plain",
                                byte_size=4,
                                sha256="a" * 64,
                                storage_key=uuid4().hex,
                                source="upload",
                                state="ready",
                                expires_at=shanghai_now(naive=True) + timedelta(days=1),
                                status=1,
                                is_deleted=0,
                            )
                            for artifact_id, owner_id, filename in (
                                (foreign_artifact_id, 1, "owner.txt"),
                                (recipient_artifact_id, 2, "recipient.txt"),
                            )
                        ]
                    )
                assert (
                    await client.post(
                        f"{base}/sessions/{recipient_session_id}/runs",
                        headers=recipient,
                        json={
                            "input_text": "read owner file",
                            "request_key": "share-foreign-file",
                            "artifact_ids": [foreign_artifact_id],
                            "context_artifact_ids": [foreign_artifact_id],
                        },
                    )
                ).status_code == 404
                submitted = await client.post(
                    f"{base}/sessions/{recipient_session_id}/runs",
                    headers=recipient,
                    json={
                        "input_text": "hello",
                        "request_key": "share-queued",
                    },
                )
                assert submitted.status_code == 201, submitted.text
                run_id = submitted.json()["data"]["id"]
                assert (await client.get(f"/api/v1/runs/{run_id}", headers=owner)).status_code == 404
                assert (await client.get(f"/api/v1/runs/{run_id}", headers=recipient)).status_code == 200

                revoked = await client.delete(f"{base}/shares/2", headers=owner)
                assert revoked.status_code == 200, revoked.text
                assert (await client.get("/api/v1/agents/shared", headers=recipient)).json()["data"] == []
                assert (await client.get(f"{base}/sessions/{recipient_session_id}/messages", headers=recipient)).status_code == 404
                assert (await client.get(f"/api/v1/runs/{run_id}", headers=recipient)).status_code == 404
                assert (await client.get(f"/api/v1/artifacts/{recipient_artifact_id}", headers=recipient)).status_code == 200
                assert (
                    await client.post(
                        f"{base}/sessions/{recipient_session_id}/runs",
                        headers=recipient,
                        json={
                            "input_text": "again",
                            "request_key": "share-revoked",
                        },
                    )
                ).status_code == 404
                assert (await client.delete(f"{base}/shares/2", headers=owner)).status_code == 404
                async with sessions() as db:
                    from app.models.agent_run import AgentRun

                    assert (await db.get(AgentRun, run_id)).state == "cancelled"
                    assert (await db.scalars(select(AgentShare))).all() == []
                    actions = (await db.scalars(select(AuditEvent.action).where(AuditEvent.target_id == 2))).all()
                    assert {"agent.shared", "agent.share_revoked"} <= set(actions)
                assert (
                    await client.post(
                        f"{base}/shares",
                        headers=owner,
                        json={
                            "user_id": 2,
                        },
                    )
                ).status_code == 200
                assert (await client.get(f"{base}/sessions/{recipient_session_id}", headers=recipient)).status_code == 200
                assert (await client.delete(base, headers=owner)).status_code == 200
                assert (await client.get("/api/v1/agents/shared", headers=recipient)).json()["data"] == []
                assert (await client.get(f"{base}/sessions/{recipient_session_id}", headers=recipient)).status_code == 404
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            await engine.dispose()

    asyncio.run(exercise())
