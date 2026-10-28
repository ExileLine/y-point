# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : test_auth.py

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.password import hash_password
from app.core.security import ACCESS_TOKEN_SECONDS
from app.db.session import get_db_session
from app.main import app
from app.models import Admin, AuthAttempt
from app.models.base import Base
from app.schemas.auth import (
    AuditOut,
    LoginIn,
    PasswordChangeIn,
    PasswordResetIn,
    ProfileUpdateIn,
    RefreshIn,
    TokenOut,
    UserCreateIn,
    UserOut,
    UserPage,
    UserUpdateIn,
)


PASSWORD = "StrongPassword123"
NEW_PASSWORD = "AnotherPassword456"


def response_data(response: httpx.Response) -> dict | list:
    """读取统一 API 响应中的业务数据并检查成功业务码。"""
    body = response.json()
    assert body["code"] in {200, 201}
    return body["data"]


async def exercise_auth(database: Path | URL) -> None:
    """验证认证、管理员操作与令牌撤销的完整 HTTP 流程。"""
    engine = create_async_engine(database if isinstance(database, URL) else f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add(Admin(username="root", mail="root@example.com", password=hash_password(PASSWORD), role="admin", status=1, is_deleted=0))
        await db.commit()

    async def test_session():
        """为测试路由提供临时数据库会话。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "root",
                    "password": PASSWORD,
                },
            )
            assert login.status_code == 200, login.text
            assert login.json()["message"] == "操作成功"
            root_tokens = response_data(login)
            assert root_tokens["expires_in"] == ACCESS_TOKEN_SECONDS == 604800
            root_payload = jwt.decode(root_tokens["access_token"], get_config().SECRET_KEY, algorithms=["HS256"])
            assert root_payload["exp"] - root_payload["iat"] == ACCESS_TOKEN_SECONDS
            root_headers = {
                "Authorization": f"Bearer {root_tokens['access_token']}",
            }
            assert response_data(await client.get("/api/v1/auth/me", headers=root_headers))["role"] == "admin"

            created = await client.post(
                "/api/v1/users",
                headers=root_headers,
                json={
                    "username": "Alice",
                    "mail": "alice@example.com",
                    "password": PASSWORD,
                },
            )
            assert created.status_code == 201, created.text
            alice_id = response_data(created)["id"]
            assert "password" not in response_data(created)
            duplicate = await client.post(
                "/api/v1/users",
                headers=root_headers,
                json={
                    "username": "alice",
                    "password": PASSWORD,
                },
            )
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == 10003

            alice_login = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "ALICE@example.com",
                    "password": PASSWORD,
                },
            )
            assert alice_login.status_code == 200, alice_login.text
            alice_tokens = response_data(alice_login)
            alice_headers = {
                "Authorization": f"Bearer {alice_tokens['access_token']}",
            }
            forbidden = await client.get("/api/v1/users", headers=alice_headers)
            assert forbidden.status_code == 403
            assert forbidden.json()["code"] == 403
            assert (await client.get("/api/v1/users/1", headers=alice_headers)).status_code == 403
            assert (await client.get("/api/v1/users/audit/events", headers=alice_headers)).status_code == 403
            updated_profile = await client.put(
                "/api/v1/auth/me",
                headers=alice_headers,
                json={
                    "nickname": "Alice",
                },
            )
            assert response_data(updated_profile)["nickname"] == "Alice"

            rotated = await client.post(
                "/api/v1/auth/refresh",
                json={
                    "refresh_token": alice_tokens["refresh_token"],
                },
            )
            assert rotated.status_code == 200, rotated.text
            rotated_data = response_data(rotated)
            assert rotated_data["refresh_token"] != alice_tokens["refresh_token"]
            assert rotated_data["expires_in"] == ACCESS_TOKEN_SECONDS
            rotated_payload = jwt.decode(rotated_data["access_token"], get_config().SECRET_KEY, algorithms=["HS256"])
            assert rotated_payload["exp"] - rotated_payload["iat"] == ACCESS_TOKEN_SECONDS
            assert (
                await client.post(
                    "/api/v1/auth/refresh",
                    json={
                        "refresh_token": alice_tokens["refresh_token"],
                    },
                )
            ).status_code == 401

            assert (
                await client.post(
                    "/api/v1/auth/password",
                    headers=alice_headers,
                    json={
                        "current_password": PASSWORD,
                        "new_password": NEW_PASSWORD,
                    },
                )
            ).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            rotated_token = response_data(rotated)["refresh_token"]
            assert (
                await client.post(
                    "/api/v1/auth/refresh",
                    json={
                        "refresh_token": rotated_token,
                    },
                )
            ).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": "alice",
                        "password": PASSWORD,
                    },
                )
            ).status_code == 401
            relogin = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "alice",
                    "password": NEW_PASSWORD,
                },
            )
            assert relogin.status_code == 200
            alice_headers = {
                "Authorization": f"Bearer {response_data(relogin)['access_token']}",
            }

            reset = await client.post(
                f"/api/v1/users/{alice_id}/reset-password",
                headers=root_headers,
                json={
                    "new_password": PASSWORD,
                },
            )
            assert reset.status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            relogin = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "alice",
                    "password": PASSWORD,
                },
            )
            assert relogin.status_code == 200
            alice_headers = {
                "Authorization": f"Bearer {response_data(relogin)['access_token']}",
            }

            assert (
                await client.put(
                    f"/api/v1/users/{alice_id}",
                    headers=root_headers,
                    json={
                        "role": "admin",
                    },
                )
            ).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            relogin = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "alice",
                    "password": PASSWORD,
                },
            )
            assert relogin.status_code == 200
            alice_headers = {
                "Authorization": f"Bearer {response_data(relogin)['access_token']}",
            }
            assert (await client.get("/api/v1/users", headers=alice_headers)).status_code == 200

            assert (
                await client.put(
                    f"/api/v1/users/{alice_id}",
                    headers=root_headers,
                    json={
                        "role": "user",
                    },
                )
            ).status_code == 200
            assert (await client.get("/api/v1/users", headers=alice_headers)).status_code == 401
            relogin = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "alice",
                    "password": PASSWORD,
                },
            )
            assert relogin.status_code == 200
            alice_headers = {
                "Authorization": f"Bearer {response_data(relogin)['access_token']}",
            }

            assert (
                await client.put(
                    f"/api/v1/users/{alice_id}",
                    headers=root_headers,
                    json={
                        "status": 0,
                    },
                )
            ).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            assert (
                await client.put(
                    "/api/v1/users/1",
                    headers=root_headers,
                    json={
                        "status": 0,
                    },
                )
            ).status_code == 409
            events = await client.get("/api/v1/users/audit/events", headers=root_headers)
            assert events.status_code == 200
            assert any(event["action"] == "auth.password_changed" for event in response_data(events))

            assert (await client.post("/api/v1/auth/logout", headers=root_headers)).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=root_headers)).status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/refresh",
                    json={
                        "refresh_token": root_tokens["refresh_token"],
                    },
                )
            ).status_code == 401
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def exercise_login_limit(database: Path | URL) -> None:
    """验证连续登录失败后的限流响应。"""
    engine = create_async_engine(database if isinstance(database, URL) else f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def test_session():
        """为限流测试提供临时数据库会话。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for _ in range(5):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": "unknown",
                        "password": "wrong",
                    },
                )
                assert response.status_code == 401
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": "unknown",
                        "password": "wrong",
                    },
                )
            ).status_code == 429
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def exercise_concurrent_login_limit(database: URL) -> None:
    """验证并发错误登录不会丢失计数或抛出数据库异常。"""
    engine = create_async_engine(database)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def test_session():
        """为并发请求提供独立数据库事务。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

            async def failed_login() -> httpx.Response:
                """以相同账号和来源地址提交错误凭据。"""
                return await client.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": "parallel-unknown",
                        "password": "wrong",
                    },
                )

            async def assert_counts() -> None:
                """检查账号和来源地址的失败次数都没有丢失。"""
                account_id = hashlib.sha256(b"account:parallel-unknown").hexdigest()
                address_id = hashlib.sha256(b"ip:127.0.0.1").hexdigest()
                async with sessions() as db:
                    rows = (await db.scalars(select(AuthAttempt).where(AuthAttempt.id.in_([account_id, address_id])))).all()
                    assert len(rows) == 2
                    assert all(row.failures == 5 and row.locked_until is not None for row in rows)

            responses = await asyncio.gather(*(failed_login() for _ in range(5)))
            assert [response.status_code for response in responses] == [401] * 5
            assert (await failed_login()).status_code == 429
            await assert_counts()

            async with sessions.begin() as db:
                await db.execute(delete(AuthAttempt))
            assert (await failed_login()).status_code == 401
            responses = await asyncio.gather(*(failed_login() for _ in range(4)))
            assert [response.status_code for response in responses] == [401] * 4
            assert (await failed_login()).status_code == 429
            await assert_counts()
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def test_auth_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行认证与用户管理的 HTTP 集成用例。"""
    monkeypatch.setattr(get_config(), "SECRET_KEY", "test-only-jwt-signing-key-32-bytes-long")
    asyncio.run(exercise_auth(tmp_path / "auth.db"))


def test_login_rate_limit(tmp_path: Path) -> None:
    """运行数据库登录限流用例。"""
    asyncio.run(exercise_login_limit(tmp_path / "rate.db"))


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_POSTGRES_M1") != "1", reason="需显式启用隔离 PostgreSQL M1 验收")
def test_postgres_m1_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """在隔离 PostgreSQL 库验证迁移、用户管理、撤权与登录限流。"""
    asyncio.run(exercise_isolated_m1_auth("postgres", make_url(get_config().pg_async_url), "postgres", monkeypatch))


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_MYSQL_M1") != "1", reason="需显式启用隔离 MySQL M1 验收")
def test_mysql_m1_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """在隔离 MySQL 库验证迁移、用户管理、撤权与登录限流。"""
    asyncio.run(exercise_isolated_m1_auth("mysql", make_url(get_config().mysql_async_url), None, monkeypatch))


async def exercise_isolated_m1_auth(backend: str, base_url: URL, admin_database: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """在随机测试库验收 M1 关键 HTTP 流程和并发失败计数。"""
    monkeypatch.setattr(get_config(), "SECRET_KEY", "test-only-jwt-signing-key-32-bytes-long")
    admin_engine = create_async_engine(base_url.set(database=admin_database), isolation_level="AUTOCOMMIT")
    database_name = f"yp_m1_auth_{uuid4().hex[:12]}"
    identifier = f'"{database_name}"' if backend == "postgres" else f"`{database_name}`"
    created = False
    try:
        async with admin_engine.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE {identifier}"))
        created = True
        env = os.environ.copy()
        env.update(
            {
                "DB_BACKEND": backend,
                "POSTGRESQL_DATABASE" if backend == "postgres" else "MYSQL_DATABASE": database_name,
            }
        )
        root = Path(__file__).resolve().parents[1]
        for args in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head"), ("check",)):
            result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        database_url = base_url.set(database=database_name)
        await exercise_auth(database_url)
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(delete(AuthAttempt))
        finally:
            await engine.dispose()
        await exercise_login_limit(database_url)
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(delete(AuthAttempt))
        finally:
            await engine.dispose()
        await exercise_concurrent_login_limit(database_url)
    finally:
        if created:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f"DROP DATABASE {identifier}"))
        await admin_engine.dispose()


def test_m1_route_summaries() -> None:
    """确认 M1 路由仅使用约定的 HTTP 方法并标有中文摘要。"""
    paths = app.openapi()["paths"]
    for path, methods in paths.items():
        if path.startswith(("/api/v1/auth", "/api/v1/users")):
            for method, operation in methods.items():
                assert method in {"get", "post", "put", "delete"}
                assert operation["summary"]
                assert any("\u4e00" <= char <= "\u9fff" for char in operation["summary"])


def test_docs_expose_bearer_authorization() -> None:
    """确认文档通过安全方案而非普通请求头发送访问令牌。"""
    schema = app.openapi()
    schemes = schema["components"]["securitySchemes"]
    assert any(value.get("type") == "http" and value.get("scheme") == "bearer" for value in schemes.values())
    operation = schema["paths"]["/api/v1/auth/me"]["get"]
    assert operation["security"]
    assert all(parameter.get("name", "").lower() != "authorization" for parameter in operation.get("parameters", []))


def test_auth_schema_documentation() -> None:
    """确认认证字段的中文说明和示例出现在模型及接口文档中。"""
    models = (
        LoginIn,
        RefreshIn,
        PasswordChangeIn,
        PasswordResetIn,
        TokenOut,
        UserCreateIn,
        UserUpdateIn,
        ProfileUpdateIn,
        UserOut,
        UserPage,
        AuditOut,
    )
    for model in models:
        properties = model.model_json_schema()["properties"]
        for name, field in model.model_fields.items():
            assert any("\u4e00" <= char <= "\u9fff" for char in properties[name]["description"])
            assert properties[name]["examples"] == field.examples
            assert field.examples

    schemas = app.openapi()["components"]["schemas"]
    for model in (LoginIn, RefreshIn, PasswordChangeIn, PasswordResetIn, UserCreateIn, UserUpdateIn, ProfileUpdateIn):
        for name, field in model.model_fields.items():
            documented = schemas[model.__name__]["properties"][name]
            assert documented["description"] == field.description
            assert documented["examples"] == field.examples
