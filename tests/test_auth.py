# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : test_auth.py

import asyncio
from pathlib import Path

import httpx
import jwt
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.password import hash_password
from app.core.security import ACCESS_TOKEN_SECONDS
from app.db.session import get_db_session
from app.main import app
from app.models import Admin
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


async def exercise_auth(database: Path) -> None:
    """验证认证、管理员操作与令牌撤销的完整 HTTP 流程。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
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
            login = await client.post("/api/v1/auth/login", json={"identifier": "root", "password": PASSWORD})
            assert login.status_code == 200, login.text
            assert login.json()["message"] == "操作成功"
            root_tokens = response_data(login)
            assert root_tokens["expires_in"] == ACCESS_TOKEN_SECONDS == 604800
            root_payload = jwt.decode(root_tokens["access_token"], get_config().SECRET_KEY, algorithms=["HS256"])
            assert root_payload["exp"] - root_payload["iat"] == ACCESS_TOKEN_SECONDS
            root_headers = {"Authorization": f"Bearer {root_tokens['access_token']}"}
            assert response_data(await client.get("/api/v1/auth/me", headers=root_headers))["role"] == "admin"

            created = await client.post(
                "/api/v1/users", headers=root_headers,
                json={"username": "Alice", "mail": "alice@example.com", "password": PASSWORD},
            )
            assert created.status_code == 201, created.text
            alice_id = response_data(created)["id"]
            assert "password" not in response_data(created)
            duplicate = await client.post("/api/v1/users", headers=root_headers, json={"username": "alice", "password": PASSWORD})
            assert duplicate.status_code == 409
            assert duplicate.json()["code"] == 10003

            alice_login = await client.post("/api/v1/auth/login", json={"identifier": "ALICE@example.com", "password": PASSWORD})
            assert alice_login.status_code == 200, alice_login.text
            alice_tokens = response_data(alice_login)
            alice_headers = {"Authorization": f"Bearer {alice_tokens['access_token']}"}
            forbidden = await client.get("/api/v1/users", headers=alice_headers)
            assert forbidden.status_code == 403
            assert forbidden.json()["code"] == 403
            assert (await client.get("/api/v1/users/1", headers=alice_headers)).status_code == 403
            assert (await client.get("/api/v1/users/audit/events", headers=alice_headers)).status_code == 403
            updated_profile = await client.put("/api/v1/auth/me", headers=alice_headers, json={"nickname": "Alice"})
            assert response_data(updated_profile)["nickname"] == "Alice"

            rotated = await client.post("/api/v1/auth/refresh", json={"refresh_token": alice_tokens["refresh_token"]})
            assert rotated.status_code == 200, rotated.text
            rotated_data = response_data(rotated)
            assert rotated_data["refresh_token"] != alice_tokens["refresh_token"]
            assert rotated_data["expires_in"] == ACCESS_TOKEN_SECONDS
            rotated_payload = jwt.decode(rotated_data["access_token"], get_config().SECRET_KEY, algorithms=["HS256"])
            assert rotated_payload["exp"] - rotated_payload["iat"] == ACCESS_TOKEN_SECONDS
            assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": alice_tokens["refresh_token"]})).status_code == 401

            assert (await client.post(
                "/api/v1/auth/password", headers=alice_headers,
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            )).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            rotated_token = response_data(rotated)["refresh_token"]
            assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": rotated_token})).status_code == 401
            assert (await client.post("/api/v1/auth/login", json={"identifier": "alice", "password": PASSWORD})).status_code == 401
            relogin = await client.post("/api/v1/auth/login", json={"identifier": "alice", "password": NEW_PASSWORD})
            assert relogin.status_code == 200
            alice_headers = {"Authorization": f"Bearer {response_data(relogin)['access_token']}"}

            reset = await client.post(f"/api/v1/users/{alice_id}/reset-password", headers=root_headers, json={"new_password": PASSWORD})
            assert reset.status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            relogin = await client.post("/api/v1/auth/login", json={"identifier": "alice", "password": PASSWORD})
            assert relogin.status_code == 200
            alice_headers = {"Authorization": f"Bearer {response_data(relogin)['access_token']}"}

            assert (await client.put(f"/api/v1/users/{alice_id}", headers=root_headers, json={"role": "admin"})).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            relogin = await client.post("/api/v1/auth/login", json={"identifier": "alice", "password": PASSWORD})
            assert relogin.status_code == 200
            alice_headers = {"Authorization": f"Bearer {response_data(relogin)['access_token']}"}
            assert (await client.get("/api/v1/users", headers=alice_headers)).status_code == 200

            assert (await client.put(f"/api/v1/users/{alice_id}", headers=root_headers, json={"role": "user"})).status_code == 200
            assert (await client.get("/api/v1/users", headers=alice_headers)).status_code == 401
            relogin = await client.post("/api/v1/auth/login", json={"identifier": "alice", "password": PASSWORD})
            assert relogin.status_code == 200
            alice_headers = {"Authorization": f"Bearer {response_data(relogin)['access_token']}"}

            assert (await client.put(f"/api/v1/users/{alice_id}", headers=root_headers, json={"status": 0})).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=alice_headers)).status_code == 401
            assert (await client.put("/api/v1/users/1", headers=root_headers, json={"status": 0})).status_code == 409
            events = await client.get("/api/v1/users/audit/events", headers=root_headers)
            assert events.status_code == 200
            assert any(event["action"] == "auth.password_changed" for event in response_data(events))

            assert (await client.post("/api/v1/auth/logout", headers=root_headers)).status_code == 200
            assert (await client.get("/api/v1/auth/me", headers=root_headers)).status_code == 401
            assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": root_tokens["refresh_token"]})).status_code == 401
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def exercise_login_limit(database: Path) -> None:
    """验证连续登录失败后的限流响应。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
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
                response = await client.post("/api/v1/auth/login", json={"identifier": "unknown", "password": "wrong"})
                assert response.status_code == 401
            assert (await client.post("/api/v1/auth/login", json={"identifier": "unknown", "password": "wrong"})).status_code == 429
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
        LoginIn, RefreshIn, PasswordChangeIn, PasswordResetIn, TokenOut, UserCreateIn,
        UserUpdateIn, ProfileUpdateIn, UserOut, UserPage, AuditOut,
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
