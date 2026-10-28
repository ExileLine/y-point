# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:53:53
# @Author  : yangyuexiong
# @File    : test_provider_catalog.py

import asyncio
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from langchain_anthropic import ChatAnthropic
from langchain_openai import ChatOpenAI
from loguru import logger
from sqlalchemy import select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.api.v1.providers as providers_api
import app.api.v1.models as models_api
from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models import Admin, ModelCallRecord, ModelCatalog, ProviderConnection
from app.models.base import Base
from app.schemas.provider import (
    ConnectionTestOut,
    CapabilityTestIn,
    CapabilityTestOut,
    DiscoveredModelOut,
    ModelCreateIn,
    ModelCallOut,
    ModelCallPageOut,
    ModelDiscoveryOut,
    ModelImportIn,
    ModelImportItem,
    ModelImportOut,
    ModelOut,
    ModelTestOut,
    ModelUpdateIn,
    ProviderCreateIn,
    ProviderKindOut,
    ProviderOut,
    ProviderUpdateIn,
)
from app.services.provider_credentials import decrypt_credentials, encrypt_credentials
from app.services.model_adapter import build_chat_model
from app.services.model_probe import probe_model
from app.services.provider_probe import normalize_base_url, probe_provider


PASSWORD = "StrongPassword123"


async def exercise_catalog(database: Path | str | URL, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证供应商和模型的管理员权限、密文存储及关联规则。"""
    database_url = database if isinstance(database, (str, URL)) else f"sqlite+aiosqlite:///{database}"
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as db:
        db.add_all([
            Admin(username="root", password=hash_password(PASSWORD), role="admin", status=1, is_deleted=0),
            Admin(username="reader", password=hash_password(PASSWORD), role="user", status=1, is_deleted=0),
        ])
        await db.commit()

    async def test_session():
        """为目录接口提供隔离的临时数据库会话。"""
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            root_login = await client.post("/api/v1/auth/login", json={"identifier": "root", "password": PASSWORD})
            user_login = await client.post("/api/v1/auth/login", json={"identifier": "reader", "password": PASSWORD})
            root_headers = {"Authorization": f"Bearer {root_login.json()['data']['access_token']}"}
            user_headers = {"Authorization": f"Bearer {user_login.json()['data']['access_token']}"}
            assert (await client.get("/api/v1/providers", headers=user_headers)).status_code == 403
            assert (await client.get("/api/v1/providers/kinds", headers=user_headers)).status_code == 403
            assert (await client.get("/api/v1/models", headers=user_headers)).status_code == 403
            assert (await client.post("/api/v1/providers", headers=user_headers, json={})).status_code == 403
            kinds = await client.get("/api/v1/providers/kinds", headers=root_headers)
            assert kinds.status_code == 200
            kind_data = {item["kind"]: item for item in kinds.json()["data"]}
            assert set(kind_data) == {"openai_compatible", "openai", "anthropic", "ollama", "azure_openai", "gemini", "bedrock"}
            assert "api_key" in kind_data["anthropic"]["required_fields"]
            assert "api_version" in kind_data["anthropic"]["optional_fields"]
            assert "api_version" not in kind_data["ollama"]["optional_fields"]
            assert kind_data["ollama"]["suggested_base_url"] == "http://127.0.0.1:11434/v1"
            assert "api_version" in kind_data["azure_openai"]["required_fields"]
            assert "api_key" in kind_data["gemini"]["required_fields"]
            assert "region" in kind_data["bedrock"]["required_fields"]
            assert "api_key" not in kind_data["bedrock"]["optional_fields"]

            captured_logs: list[str] = []

            def capture_log(message) -> None:
                """收集本次创建请求的实际日志以检查敏感值。"""
                captured_logs.append(message.record["message"])

            sink_id = logger.add(capture_log, level="INFO")
            try:
                created = await client.post("/api/v1/providers", headers=root_headers, json={
                    "name": "Test Provider", "kind": "openai_compatible", "base_url": "http://models.example/v1",
                    "api_key": "top-secret-credential", "custom_headers": {"X-Tenant": "tenant-secret"},
                })
            finally:
                logger.remove(sink_id)
            assert created.status_code == 201, created.text
            assert "top-secret-credential" not in created.text and "tenant-secret" not in created.text
            assert all("top-secret-credential" not in line and "tenant-secret" not in line for line in captured_logs)
            provider_id = created.json()["data"]["id"]
            assert created.json()["data"]["has_api_key"] is True
            assert created.json()["data"]["has_custom_headers"] is True
            assert created.json()["data"]["base_url"] == "http://models.example/v1"
            assert (await client.get(f"/api/v1/providers/{provider_id}", headers=user_headers)).status_code == 403
            ollama = await client.post("/api/v1/providers", headers=root_headers, json={
                "name": "Local Ollama", "kind": "ollama", "base_url": "http://127.0.0.1:11434/v1",
            })
            assert ollama.status_code == 201 and ollama.json()["data"]["has_api_key"] is False
            assert (await client.delete(f"/api/v1/providers/{ollama.json()['data']['id']}", headers=root_headers)).status_code == 200

            azure_body = {
                "name": "Azure Test", "kind": "azure_openai", "base_url": "https://example.openai.azure.com",
                "api_version": "2024-10-21", "api_key": "azure-secret",
            }
            assert (await client.post("/api/v1/providers", headers=root_headers, json={**azure_body, "api_key": None})).status_code == 400
            azure = await client.post("/api/v1/providers", headers=root_headers, json=azure_body)
            assert azure.status_code == 201 and azure.json()["data"]["has_api_key"] is True
            assert "azure-secret" not in azure.text
            assert (await client.delete(f"/api/v1/providers/{azure.json()['data']['id']}", headers=root_headers)).status_code == 200

            gemini_body = {"name": "Gemini Test", "kind": "gemini", "api_key": "gemini-secret"}
            gemini = await client.post("/api/v1/providers", headers=root_headers, json=gemini_body)
            assert gemini.status_code == 201
            assert gemini.json()["data"]["base_url"] == "https://generativelanguage.googleapis.com"
            assert "gemini-secret" not in gemini.text
            assert (await client.delete(f"/api/v1/providers/{gemini.json()['data']['id']}", headers=root_headers)).status_code == 200

            bedrock_body = {"name": "Bedrock Test", "kind": "bedrock", "region": "us-east-1"}
            bedrock = await client.post("/api/v1/providers", headers=root_headers, json=bedrock_body)
            assert bedrock.status_code == 201
            assert bedrock.json()["data"]["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"
            assert bedrock.json()["data"]["has_api_key"] is False
            assert (await client.delete(f"/api/v1/providers/{bedrock.json()['data']['id']}", headers=root_headers)).status_code == 200
            invalid_bedrock = await client.post(
                "/api/v1/providers", headers=root_headers,
                json={**bedrock_body, "name": "Invalid Bedrock", "api_key": "not-used"},
            )
            assert invalid_bedrock.status_code == 400

            async def fake_probe(provider: ProviderConnection) -> ConnectionTestOut:
                """模拟上游限流，验证管理接口保留诊断结果格式。"""
                assert provider.id == provider_id
                return ConnectionTestOut(available=False, latency_ms=3, failure="rate_limit")

            monkeypatch.setattr(providers_api, "probe_provider", fake_probe)
            tested = await client.post(f"/api/v1/providers/{provider_id}/test", headers=root_headers)
            assert tested.status_code == 200 and tested.json()["data"]["failure"] == "rate_limit"
            assert tested.json()["data"]["models"] == []
            assert "top-secret-credential" not in tested.text
            failed_discovery = await client.get(f"/api/v1/providers/{provider_id}/available-models", headers=root_headers)
            assert failed_discovery.status_code == 200
            assert failed_discovery.json()["data"]["failure"] == "rate_limit"
            assert failed_discovery.json()["data"]["models"] == []

            async def fake_unavailable_list(provider: ProviderConnection) -> ConnectionTestOut:
                """模拟供应商未提供模型列表接口。"""
                return ConnectionTestOut(available=False, latency_ms=6, upstream_status_code=404, failure="model_list_unavailable")

            monkeypatch.setattr(providers_api, "probe_provider", fake_unavailable_list)
            unavailable_test = await client.post(f"/api/v1/providers/{provider_id}/test", headers=root_headers)
            unavailable_models = await client.get(f"/api/v1/providers/{provider_id}/available-models", headers=root_headers)
            assert unavailable_test.json()["data"]["failure"] == "model_list_unavailable"
            assert unavailable_test.json()["data"]["upstream_status_code"] == 404
            assert unavailable_models.json()["data"]["failure"] == "model_list_unavailable"
            assert unavailable_models.json()["data"]["upstream_status_code"] == 404
            assert unavailable_models.json()["data"]["models"] == []

            async def fake_success_probe(provider: ProviderConnection) -> ConnectionTestOut:
                """模拟上游成功响应，验证接口返回模型 ID 列表。"""
                assert provider.id == provider_id
                return ConnectionTestOut(
                    available=True, latency_ms=4, model_count=2, models=["model-1", "model-2"], model_names={"model-2": "Second Model"}
                )

            monkeypatch.setattr(providers_api, "probe_provider", fake_success_probe)
            tested = await client.post(f"/api/v1/providers/{provider_id}/test", headers=root_headers)
            assert tested.status_code == 200
            assert tested.json()["data"]["models"] == ["model-1", "model-2"]
            assert tested.json()["data"]["model_count"] == 2
            discovery_path = f"/api/v1/providers/{provider_id}/available-models"
            assert (await client.get(discovery_path, headers=user_headers)).status_code == 403
            discovered = await client.get(discovery_path, headers=root_headers)
            assert discovered.status_code == 200
            assert discovered.json()["data"]["models"] == [
                {"external_id": "model-1", "display_name": "model-1", "already_added": False},
                {"external_id": "model-2", "display_name": "Second Model", "already_added": False},
            ]
            import_body = {
                "provider_id": provider_id,
                "models": [
                    {"external_id": "model-1"},
                    {"external_id": "model-2", "display_name": "Second Model", "capabilities": ["text"], "context_window": 8192},
                ],
            }
            assert (await client.post("/api/v1/models/import", headers=user_headers, json=import_body)).status_code == 403
            imported = await client.post("/api/v1/models/import", headers=root_headers, json=import_body)
            assert imported.status_code == 201, imported.text
            imported_models = imported.json()["data"]["created"]
            imported_ids = [model["id"] for model in imported_models]
            assert [model["external_id"] for model in imported_models] == ["model-1", "model-2"]
            assert imported_models[0]["display_name"] == "model-1"
            assert imported_models[0]["capabilities"] == [] and imported_models[0]["input_price"] is None
            assert imported_models[1]["display_name"] == "Second Model"
            assert imported_models[1]["context_window"] == 8192
            assert all(model["capabilities_verified"] is False for model in imported_models)
            discovered_again = await client.get(discovery_path, headers=root_headers)
            assert all(model["already_added"] for model in discovered_again.json()["data"]["models"])
            repeated = await client.post("/api/v1/models/import", headers=root_headers, json=import_body)
            assert repeated.status_code == 200
            assert repeated.json()["data"] == {"created": [], "skipped_existing": ["model-1", "model-2"]}
            duplicate_import = await client.post("/api/v1/models/import", headers=root_headers, json={
                "provider_id": provider_id, "models": [{"external_id": "model-3"}, {"external_id": "model-3"}],
            })
            assert duplicate_import.status_code == 400
            assert (await client.delete(f"/api/v1/models/{imported_ids[0]}", headers=root_headers)).status_code == 200
            restored = await client.post("/api/v1/models/import", headers=root_headers, json={
                "provider_id": provider_id, "models": [{"external_id": "model-1", "display_name": "Restored Model"}],
            })
            assert restored.status_code == 201
            assert restored.json()["data"]["created"][0]["id"] == imported_ids[0]
            assert restored.json()["data"]["created"][0]["display_name"] == "Restored Model"
            async with sessions() as db:
                provider = await db.get(ProviderConnection, provider_id)
                assert provider.encrypted_credentials
                assert "top-secret-credential" not in provider.encrypted_credentials
                original_ciphertext = provider.encrypted_credentials

            updated = await client.put(f"/api/v1/providers/{provider_id}", headers=root_headers, json={"remark": "可用连接"})
            assert updated.status_code == 200, updated.text
            async with sessions() as db:
                provider = await db.get(ProviderConnection, provider_id)
                assert provider.encrypted_credentials == original_ciphertext
            duplicate = await client.post("/api/v1/providers", headers=root_headers, json={
                "name": "test provider", "kind": "openai_compatible", "base_url": "https://models.example/v1",
            })
            assert duplicate.status_code == 409
            assert (await client.post("/api/v1/providers", headers=root_headers, json={
                "name": "   ", "kind": "openai_compatible", "base_url": "https://models.example/v1",
            })).status_code == 400
            assert (await client.post("/api/v1/providers", headers=root_headers, json={
                "name": "Bad Headers", "kind": "openai_compatible", "base_url": "https://models.example/v1",
                "custom_headers": {"X-Tenant": "first\nsecond"},
            })).status_code == 400
            cleared = await client.put(f"/api/v1/providers/{provider_id}", headers=root_headers, json={"api_key": None})
            assert cleared.status_code == 200 and cleared.json()["data"]["has_api_key"] is False
            assert cleared.json()["data"]["has_custom_headers"] is True

            created_model = await client.post("/api/v1/models", headers=root_headers, json={
                "provider_id": provider_id, "external_id": "chat-1", "display_name": "Chat 1",
                "capabilities": ["text", "streaming"], "context_window": 8192,
            })
            assert created_model.status_code == 201, created_model.text
            model_id = created_model.json()["data"]["id"]
            assert created_model.json()["data"]["capabilities_verified"] is False
            assert created_model.json()["data"]["price_currency"] == "USD"
            assert (await client.post("/api/v1/models", headers=root_headers, json={
                "provider_id": provider_id, "external_id": "chat-1", "display_name": "Duplicate", "capabilities": ["text"],
            })).status_code == 409
            assert (await client.delete(f"/api/v1/providers/{provider_id}", headers=root_headers)).status_code == 409
            assert (await client.get(f"/api/v1/models/{model_id}", headers=user_headers)).status_code == 403
            assert (await client.post(f"/api/v1/models/{model_id}/test", headers=user_headers)).status_code == 403

            async def fake_model_probe(model: ModelCatalog, provider: ProviderConnection) -> ModelTestOut:
                """模拟模型调用成功，验证测试接口的权限和返回结构。"""
                assert model.id == model_id and provider.id == provider_id
                return ModelTestOut(
                    model_id=model.id, external_id=model.external_id, available=True, latency_ms=5, upstream_status_code=200,
                    input_tokens=10, output_tokens=5, estimated_cost=Decimal("0.000040"), price_currency="USD",
                )

            monkeypatch.setattr(models_api, "probe_model", fake_model_probe)
            model_test = await client.post(f"/api/v1/models/{model_id}/test", headers=root_headers)
            assert model_test.status_code == 200
            assert model_test.json()["data"]["available"] is True
            assert model_test.json()["data"]["upstream_status_code"] == 200
            assert model_test.json()["data"]["input_tokens"] == 10
            assert model_test.json()["data"]["estimated_cost"] == "0.000040"
            record_id = model_test.json()["data"]["record_id"]
            async with sessions() as db:
                record = await db.get(ModelCallRecord, record_id)
                assert record.actor_id == 1 and record.model_id == model_id
                assert record.estimated_cost == Decimal("0.000040000000")
                assert record.failure is None
            assert "top-secret-credential" not in model_test.text
            assert (await client.get(f"/api/v1/models/{model_id}", headers=root_headers)).json()["data"]["capabilities_verified"] is False

            async def fake_failed_model_probe(model: ModelCatalog, provider: ProviderConnection) -> ModelTestOut:
                """模拟上游限流以验证失败调用同样持久化。"""
                return ModelTestOut(
                    model_id=model.id, external_id=model.external_id, available=False, latency_ms=7,
                    upstream_status_code=429, failure="rate_limit",
                )

            monkeypatch.setattr(models_api, "probe_model", fake_failed_model_probe)
            failed_test = await client.post(f"/api/v1/models/{model_id}/test", headers=root_headers)
            assert failed_test.status_code == 200 and failed_test.json()["data"]["failure"] == "rate_limit"
            calls_path = f"/api/v1/models/{model_id}/calls"
            assert (await client.get(calls_path, headers=user_headers)).status_code == 403
            calls = await client.get(calls_path, headers=root_headers, params={"page": 1, "size": 1})
            assert calls.status_code == 200 and calls.json()["data"]["total"] == 2
            assert len(calls.json()["data"]["records"]) == 1
            assert calls.json()["data"]["records"][0]["failure"] == "rate_limit"
            assert calls.json()["data"]["records"][0]["estimated_cost"] is None
            assert "top-secret-credential" not in calls.text
            older_calls = await client.get(calls_path, headers=root_headers, params={"page": 2, "size": 1})
            assert older_calls.json()["data"]["records"][0]["estimated_cost"] == "0.000040000000"

            failing_capabilities = {"tools"}

            async def fake_capability_probe(model: ModelCatalog, provider: ProviderConnection, capability: str) -> ModelTestOut:
                """模拟逐项能力探测，检查通过和失败时的状态变化。"""
                unsupported = capability == "reasoning_summary" and provider.kind != "openai"
                return ModelTestOut(
                    model_id=model.id, external_id=model.external_id,
                    available=capability not in failing_capabilities and not unsupported,
                    latency_ms=9, input_tokens=12, output_tokens=4,
                    failure="unsupported_capability" if unsupported else "invalid_response" if capability in failing_capabilities else None,
                )

            monkeypatch.setattr(models_api, "probe_capability", fake_capability_probe)
            capability_path = f"/api/v1/models/{model_id}/capabilities/test"
            assert (await client.post(capability_path, headers=user_headers, json={"capability": "text"})).status_code == 403
            unsupported_summary = await client.post(capability_path, headers=root_headers, json={"capability": "reasoning_summary"})
            assert unsupported_summary.status_code == 200
            assert unsupported_summary.json()["data"]["result"]["failure"] == "unsupported_capability"
            assert unsupported_summary.json()["data"]["verified_capabilities"] == []
            text_probe = await client.post(capability_path, headers=root_headers, json={"capability": "text"})
            assert text_probe.status_code == 200 and text_probe.json()["data"]["verified_capabilities"] == ["text"]
            stream_probe = await client.post(capability_path, headers=root_headers, json={"capability": "streaming"})
            assert stream_probe.status_code == 200
            assert stream_probe.json()["data"]["verified_capabilities"] == ["streaming", "text"]
            verified_model = await client.get(f"/api/v1/models/{model_id}", headers=root_headers)
            assert verified_model.json()["data"]["capabilities_verified"] is True
            tool_probe = await client.post(capability_path, headers=root_headers, json={"capability": "tools"})
            assert tool_probe.json()["data"]["result"]["failure"] == "invalid_response"
            assert tool_probe.json()["data"]["verified_capabilities"] == ["streaming", "text"]
            failing_capabilities.add("text")
            failed_retest = await client.post(capability_path, headers=root_headers, json={"capability": "text"})
            assert failed_retest.json()["data"]["verified_capabilities"] == ["streaming"]
            assert (await client.get(f"/api/v1/models/{model_id}", headers=root_headers)).json()["data"]["capabilities_verified"] is False
            failing_capabilities.remove("text")
            assert (await client.post(capability_path, headers=root_headers, json={"capability": "text"})).json()["data"][
                "verified_capabilities"
            ] == ["streaming", "text"]
            structured_probe = await client.post(capability_path, headers=root_headers, json={"capability": "structured_output"})
            assert structured_probe.status_code == 200
            assert structured_probe.json()["data"]["verified_capabilities"] == ["streaming", "structured_output", "text"]
            assert (await client.get(calls_path, headers=root_headers)).json()["data"]["total"] == 9
            latest_call = await client.get(calls_path, headers=root_headers, params={"size": 1})
            assert latest_call.json()["data"]["records"][0]["call_type"] == "structured_output_probe"
            vision_probe = await client.post(capability_path, headers=root_headers, json={"capability": "vision"})
            assert vision_probe.status_code == 200
            assert vision_probe.json()["data"]["verified_capabilities"] == ["streaming", "structured_output", "text", "vision"]
            assert (await client.get(calls_path, headers=root_headers, params={"size": 1})).json()["data"]["records"][0][
                "call_type"
            ] == "vision_probe"
            assert (await client.get(calls_path, headers=root_headers)).json()["data"]["total"] == 10
            changed_provider = await client.put(
                f"/api/v1/providers/{provider_id}", headers=root_headers, json={"base_url": "https://new-models.example/v1"}
            )
            assert changed_provider.status_code == 200
            invalidated = await client.get(f"/api/v1/models/{model_id}", headers=root_headers)
            assert invalidated.json()["data"]["verified_capabilities"] == []
            assert invalidated.json()["data"]["capabilities_verified"] is False

            default = await client.post(f"/api/v1/models/{model_id}/default", headers=root_headers)
            assert default.status_code == 200 and default.json()["data"]["is_default"] is True
            async with sessions() as db:
                other_model = await db.get(ModelCatalog, imported_ids[0])
                other_model.default_slot = "global"
                with pytest.raises(IntegrityError):
                    await db.commit()
                await db.rollback()
            switched = await asyncio.gather(
                client.post(f"/api/v1/models/{model_id}/default", headers=root_headers),
                client.post(f"/api/v1/models/{imported_ids[0]}/default", headers=root_headers),
            )
            assert all(response.status_code in {200, 409} for response in switched)
            async with sessions() as db:
                defaults = (await db.scalars(select(ModelCatalog).where(ModelCatalog.is_default.is_(True)))).all()
                assert len(defaults) == 1 and defaults[0].default_slot == "global"
            disabled = await client.put(f"/api/v1/providers/{provider_id}", headers=root_headers, json={"status": 0})
            assert disabled.status_code == 200
            model = await client.get(f"/api/v1/models/{model_id}", headers=root_headers)
            assert model.json()["data"]["is_default"] is False
            async with sessions() as db:
                assert (await db.scalars(select(ModelCatalog).where(ModelCatalog.default_slot == "global"))).all() == []
            assert (await client.post(f"/api/v1/models/{model_id}/test", headers=root_headers)).status_code == 409
            assert (await client.post(f"/api/v1/models/{model_id}/default", headers=root_headers)).status_code == 409
            assert (await client.delete(f"/api/v1/models/{model_id}", headers=root_headers)).status_code == 200
            assert (await client.get(calls_path, headers=root_headers)).json()["data"]["total"] == 10
            for imported_id in imported_ids:
                assert (await client.delete(f"/api/v1/models/{imported_id}", headers=root_headers)).status_code == 200
            assert (await client.delete(f"/api/v1/providers/{provider_id}", headers=root_headers)).status_code == 200
            assert (await client.get(f"/api/v1/providers/{provider_id}", headers=root_headers)).status_code == 404
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


async def exercise_probe() -> None:
    """使用模拟上游验证连接测试的授权头和故障分类。"""
    provider = ProviderConnection(
        name="mock", name_key="mock", kind="openai", base_url="https://models.example/v1",
        timeout_seconds=5, encrypted_credentials=encrypt_credentials("probe-secret", {"X-Tenant": "private-value"}),
    )

    def success(request: httpx.Request) -> httpx.Response:
        """检查发往上游的认证头并返回模型列表。"""
        assert request.headers["authorization"] == "Bearer probe-secret"
        assert request.headers["x-tenant"] == "private-value"
        assert str(request.url) == "https://models.example/v1/models"
        return httpx.Response(200, json={"data": [{"id": "model-1"}, {"id": "model-2", "display_name": "Second Model"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
        result = await probe_provider(provider, client)
        assert result.available is True and result.model_count == 2
        assert result.upstream_status_code == 200
        assert result.models == ["model-1", "model-2"]
        assert result.model_names == {"model-2": "Second Model"}
        assert result.latency_ms >= 0

    provider.kind = "ollama"
    provider.encrypted_credentials = encrypt_credentials(None, {})

    def ollama_models(request: httpx.Request) -> httpx.Response:
        """按 Ollama 的 OpenAI 兼容模型列表格式验证无密钥发现。"""
        assert str(request.url) == "https://models.example/v1/models"
        assert request.headers.get("authorization") is None
        return httpx.Response(200, json={
            "object": "list", "data": [
                {"id": "qwen3.8:latest", "object": "model", "created": 1788138025, "owned_by": "library"},
                {"id": "deepseek-r1:1.5b", "object": "model", "created": 1779964005, "owned_by": "library"},
            ],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(ollama_models)) as client:
        result = await probe_provider(provider, client)
        assert result.available is True and result.model_count == 2
        assert result.models == ["qwen3.8:latest", "deepseek-r1:1.5b"]
        assert result.model_names == {}

    provider.kind = "openai"
    provider.encrypted_credentials = encrypt_credentials("probe-secret", {"X-Tenant": "private-value"})

    provider_attempts = 0

    def retry_provider(request: httpx.Request) -> httpx.Response:
        """模拟模型列表接口首次网关故障、再次请求成功。"""
        nonlocal provider_attempts
        provider_attempts += 1
        return httpx.Response(502) if provider_attempts == 1 else success(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(retry_provider)) as client:
        assert (await probe_provider(provider, client)).available is True
    assert provider_attempts == 2

    auth_attempts = 0

    def reject_provider(request: httpx.Request) -> httpx.Response:
        """确认认证失败不会触发重试。"""
        nonlocal auth_attempts
        auth_attempts += 1
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject_provider)) as client:
        assert (await probe_provider(provider, client)).failure == "authentication"
    assert auth_attempts == 1

    def invalid_models(request: httpx.Request) -> httpx.Response:
        """返回缺少有效模型 ID 的上游列表。"""
        return httpx.Response(200, json={"data": [{"id": "model-1"}, {"id": "  "}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_models)) as client:
        result = await probe_provider(provider, client)
        assert result.available is False and result.failure == "invalid_response"
        assert result.models == []

    for status, expected in [
        (401, "authentication"), (429, "rate_limit"), (404, "model_list_unavailable"),
        (405, "model_list_unavailable"), (501, "model_list_unavailable"),
        (503, "upstream"), (302, "invalid_response"),
    ]:
        def failure(request: httpx.Request, status_code: int = status) -> httpx.Response:
            """返回指定上游状态供故障分类断言。"""
            return httpx.Response(status_code, text="probe-secret must stay private")

        async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as client:
            result = await probe_provider(provider, client)
            assert result.available is False and result.failure == expected
            assert result.upstream_status_code == status
            assert result.models == []
            assert "probe-secret" not in result.model_dump_json()

    def timeout(request: httpx.Request) -> httpx.Response:
        """模拟上游读超时。"""
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        result = await probe_provider(provider, client)
        assert result.failure == "timeout" and result.upstream_status_code is None

    def network_error(request: httpx.Request) -> httpx.Response:
        """模拟无法连接到供应商并检查敏感错误不会返回。"""
        raise httpx.ConnectError("probe-secret must stay private", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(network_error)) as client:
        result = await probe_provider(provider, client)
        assert result.failure == "network" and result.models == [] and result.upstream_status_code is None
        assert "probe-secret" not in result.model_dump_json()


async def exercise_model_probe() -> None:
    """验证模型测试的供应商请求格式、故障分类与凭据脱敏。"""
    model = ModelCatalog(
        id=7, provider_id=1, external_id="chat-1", display_name="Chat 1",
        input_price=Decimal("2.000000"), output_price=Decimal("4.000000"), price_currency="USD",
    )
    provider = ProviderConnection(
        id=1, name="mock", name_key="mock", kind="openai_compatible", base_url="https://models.example/v1",
        timeout_seconds=5, encrypted_credentials=encrypt_credentials("probe-secret", {"X-Tenant": "private-value"}),
    )

    def compatible_success(request: httpx.Request) -> httpx.Response:
        """检查 OpenAI 兼容协议的最小聊天请求。"""
        assert str(request.url) == "https://models.example/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer probe-secret"
        assert request.headers["x-tenant"] == "private-value"
        assert json.loads(request.content)["model"] == "chat-1"
        assert json.loads(request.content)["max_tokens"] == 1
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "pong"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(compatible_success)) as client:
        result = await probe_model(model, provider, client)
        assert result.available is True and result.failure is None
        assert result.upstream_status_code == 200 and result.latency_ms >= 0
        assert result.input_tokens == 10 and result.output_tokens == 5
        assert result.estimated_cost == Decimal("0.000040") and result.price_currency == "USD"
        assert result.model_dump(mode="json")["estimated_cost"] == "0.000040"
        assert "pong" not in result.model_dump_json()

    model_attempts = 0

    def retry_model(request: httpx.Request) -> httpx.Response:
        """模拟聊天接口首次网关故障、再次请求成功。"""
        nonlocal model_attempts
        model_attempts += 1
        return httpx.Response(502) if model_attempts == 1 else compatible_success(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(retry_model)) as client:
        assert (await probe_model(model, provider, client)).available is True
    assert model_attempts == 2

    provider.kind = "openai"

    def openai_success(request: httpx.Request) -> httpx.Response:
        """检查官方 OpenAI 请求使用当前的输出令牌参数。"""
        assert json.loads(request.content)["max_completion_tokens"] == 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(openai_success)) as client:
        result = await probe_model(model, provider, client)
        assert result.available is True
        assert result.input_tokens is None and result.output_tokens is None
        assert result.estimated_cost is None and result.price_currency is None

    provider.kind = "openai_compatible"
    model.output_price = None
    async with httpx.AsyncClient(transport=httpx.MockTransport(compatible_success)) as client:
        result = await probe_model(model, provider, client)
        assert result.input_tokens == 10 and result.output_tokens == 5
        assert result.estimated_cost is None and result.price_currency is None
    model.output_price = Decimal("4.000000")

    provider.kind = "ollama"
    provider.encrypted_credentials = encrypt_credentials(None, {})

    def ollama_success(request: httpx.Request) -> httpx.Response:
        """检查 Ollama 的 OpenAI 兼容请求无需 API 密钥。"""
        assert request.headers.get("authorization") is None
        assert str(request.url) == "https://models.example/v1/chat/completions"
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(ollama_success)) as client:
        assert (await probe_model(model, provider, client)).available is True

    provider.kind = "anthropic"
    provider.encrypted_credentials = encrypt_credentials("probe-secret", {"X-Tenant": "private-value"})

    def anthropic_success(request: httpx.Request) -> httpx.Response:
        """检查 Anthropic Messages 请求的路径和认证头。"""
        assert str(request.url) == "https://models.example/v1/messages"
        assert request.headers["x-api-key"] == "probe-secret"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert json.loads(request.content)["max_tokens"] == 1
        return httpx.Response(200, json={
            "content": [{"type": "text", "text": "pong"}], "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(anthropic_success)) as client:
        result = await probe_model(model, provider, client)
        assert result.available is True and result.estimated_cost == Decimal("0.000040")

    provider.kind = "openai_compatible"
    for status, expected in [
        (401, "authentication"), (404, "model_not_found"), (400, "invalid_request"),
        (429, "rate_limit"), (503, "upstream"), (302, "invalid_response"),
    ]:
        def failure(request: httpx.Request, status_code: int = status) -> httpx.Response:
            """返回指定上游状态和敏感响应正文。"""
            return httpx.Response(status_code, text="probe-secret must stay private")

        async with httpx.AsyncClient(transport=httpx.MockTransport(failure)) as client:
            result = await probe_model(model, provider, client)
            assert result.available is False and result.failure == expected
            assert result.upstream_status_code == status
            assert "probe-secret" not in result.model_dump_json()

    def invalid_response(request: httpx.Request) -> httpx.Response:
        """模拟上游成功状态但响应结构无效。"""
        return httpx.Response(200, json={"choices": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(invalid_response)) as client:
        assert (await probe_model(model, provider, client)).failure == "invalid_response"

    def timeout(request: httpx.Request) -> httpx.Response:
        """模拟模型调用超时。"""
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        result = await probe_model(model, provider, client)
        assert result.failure == "timeout" and result.upstream_status_code is None

    def network_error(request: httpx.Request) -> httpx.Response:
        """模拟无法连接到上游模型服务。"""
        raise httpx.ConnectError("probe-secret must stay private", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(network_error)) as client:
        result = await probe_model(model, provider, client)
        assert result.failure == "network" and result.upstream_status_code is None
        assert "probe-secret" not in result.model_dump_json()


def test_provider_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行供应商和模型管理的 HTTP 集成用例。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    asyncio.run(exercise_catalog(tmp_path / "catalog.db", monkeypatch))


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_POSTGRES_M2") != "1", reason="需显式启用隔离 PostgreSQL M2 验收")
def test_postgres_m2_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """在随机 PostgreSQL 数据库验收迁移和供应商/模型 HTTP 流程。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

    async def exercise() -> None:
        """创建隔离库，运行迁移与业务测试后删除该库。"""
        base_url = make_url(get_config().pg_async_url)
        admin_engine = create_async_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        database_name = f"yp_m2_catalog_{uuid4().hex[:12]}"
        created = False
        try:
            async with admin_engine.connect() as connection:
                await connection.execute(text(f'CREATE DATABASE "{database_name}"'))
            created = True
            env = os.environ.copy()
            env.update({"DB_BACKEND": "postgres", "POSTGRESQL_DATABASE": database_name})
            root = Path(__file__).resolve().parents[1]
            for args in (("upgrade", "head"), ("downgrade", "-1"), ("upgrade", "head"), ("check",)):
                result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=root, env=env, capture_output=True, text=True)
                assert result.returncode == 0, result.stderr
            await exercise_catalog(base_url.set(database=database_name), monkeypatch)
        finally:
            if created:
                async with admin_engine.connect() as connection:
                    await connection.execute(text(f'DROP DATABASE "{database_name}"'))
            await admin_engine.dispose()

    asyncio.run(exercise())


def test_provider_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """检查探测结果不会暴露上游响应或凭据。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    asyncio.run(exercise_probe())


def test_model_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """运行最小模型调用的协议和故障分类测试。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    asyncio.run(exercise_model_probe())


def test_model_adapter_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """检查运行时模型适配的凭据、地址及停用校验。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    provider = ProviderConnection(
        id=1, name="mock", name_key="mock", kind="openai_compatible", base_url="https://models.example/v1",
        timeout_seconds=8, status=1, is_deleted=0,
        encrypted_credentials=encrypt_credentials("probe-secret", {"X-Tenant": "private-value"}),
    )
    model = ModelCatalog(id=2, provider_id=1, external_id="chat-1", display_name="Chat 1", status=1, is_deleted=0)
    adapted = build_chat_model(model, provider)
    assert isinstance(adapted, ChatOpenAI)
    assert adapted.model_name == "chat-1" and str(adapted.openai_api_base).rstrip("/") == "https://models.example/v1"
    assert adapted.stream_usage is True and adapted.max_retries == 0
    assert adapted.default_headers["X-Tenant"] == "private-value"
    configured = build_chat_model(model, provider, temperature=Decimal("0.25"), max_output_tokens=2048)
    assert configured.temperature == 0.25 and configured.max_tokens == 2048

    provider.kind = "ollama"
    provider.base_url = "http://127.0.0.1:11434/v1"
    provider.encrypted_credentials = encrypt_credentials(None, {})
    adapted = build_chat_model(model, provider)
    assert isinstance(adapted, ChatOpenAI)
    assert str(adapted.openai_api_base).rstrip("/") == "http://127.0.0.1:11434/v1"
    assert adapted.max_retries == 0

    provider.kind = "anthropic"
    provider.base_url = "https://models.example/v1"
    provider.encrypted_credentials = encrypt_credentials("probe-secret", {"X-Tenant": "private-value"})
    provider.api_version = "2024-01-01"
    adapted = build_chat_model(model, provider)
    assert isinstance(adapted, ChatAnthropic)
    assert adapted.anthropic_api_url.rstrip("/") == "https://models.example"
    assert adapted.model == "chat-1" and adapted.max_retries == 0
    assert adapted.default_headers["anthropic-version"] == "2024-01-01"
    configured = build_chat_model(model, provider, temperature=Decimal("0.25"), max_output_tokens=2048)
    assert configured.temperature == 0.25 and configured.max_tokens == 2048

    provider.status = 0
    with pytest.raises(CustomException) as disabled:
        build_chat_model(model, provider)
    assert disabled.value.status_code == 409

    provider.status = 1
    model.provider_id = 3
    with pytest.raises(CustomException):
        build_chat_model(model, provider)


def test_provider_url_validation() -> None:
    """接受 HTTP 与 HTTPS，并拒绝其他协议及携带凭据的地址。"""
    assert normalize_base_url("http://127.0.0.1:11434/v1") == "http://127.0.0.1:11434/v1"
    assert normalize_base_url("http://models.example/v1/") == "http://models.example/v1"
    assert normalize_base_url("https://models.example/v1") == "https://models.example/v1"
    for value in ("ftp://models.example/v1", "https://user:secret@models.example/v1", "http://models.example/v1?key=secret"):
        with pytest.raises(CustomException):
            normalize_base_url(value)


def test_provider_credentials_require_stable_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少加密密钥时拒绝保存，密钥轮换后拒绝解密旧凭据。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", "")
    with pytest.raises(CustomException) as missing:
        encrypt_credentials("probe-secret", {})
    assert missing.value.status_code == 503
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    encrypted = encrypt_credentials("probe-secret", {})
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(CustomException) as rotated:
        decrypt_credentials(encrypted)
    assert rotated.value.status_code == 503


def test_m2_route_contract() -> None:
    """确认目录路由使用约定方法和中文摘要。"""
    for path, operations in app.openapi()["paths"].items():
        if path.startswith(("/api/v1/providers", "/api/v1/models")):
            for method, operation in operations.items():
                assert method in {"get", "post", "put", "delete"}
                assert any("\u4e00" <= char <= "\u9fff" for char in operation["summary"])


def test_catalog_schema_documentation() -> None:
    """确认目录字段的中文说明和示例出现在模型及接口文档中。"""
    models = (
        ProviderCreateIn, ProviderUpdateIn, ProviderOut, ProviderKindOut, ModelCreateIn, ModelUpdateIn, ModelOut,
        ModelImportItem, ModelImportIn, ModelImportOut, DiscoveredModelOut, ModelDiscoveryOut,
        ConnectionTestOut, ModelTestOut, ModelCallOut, ModelCallPageOut, CapabilityTestIn, CapabilityTestOut,
    )
    for model in models:
        properties = model.model_json_schema()["properties"]
        for name, field in model.model_fields.items():
            assert any("\u4e00" <= char <= "\u9fff" for char in properties[name]["description"])
            assert properties[name]["examples"] == field.examples
            assert field.examples

    schemas = app.openapi()["components"]["schemas"]
    for model in (ProviderCreateIn, ProviderUpdateIn, ModelCreateIn, ModelUpdateIn, ModelImportItem, ModelImportIn):
        for name, field in model.model_fields.items():
            documented = schemas[model.__name__]["properties"][name]
            assert documented["description"] == field.description
            assert documented["examples"] == field.examples
