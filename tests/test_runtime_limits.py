# -*- coding: utf-8 -*-
# @Time    : 2026-11-09 16:48:18
# @Author  : yangyuexiong
# @File    : test_runtime_limits.py

"""验证配置覆盖、参数默认值及 API、上传和 worker 的一致性。"""

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import BaseConfig, get_config
from app.core.middleware import ArtifactUploadLimitMiddleware
from app.core.security import get_current_admin
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.base import Base
from app.schemas.agent import AgentCreateIn, AgentUpdateIn
from app.schemas.provider import ProviderUpdateIn
from app.schemas.run import RunCreateIn
from app.services import message_attachments
from app.services.artifact_store import ArtifactValidationError, save_upload
from app.services.attachment_text import extract_context_text
from app.services.run_manager import create_run


def test_environment_limits_and_default_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量支持非默认值，非法或互相矛盾的配置在加载时失败。"""
    monkeypatch.setenv("ARTIFACT_UPLOAD_MAX_BYTES", "31457280")
    monkeypatch.setenv("ATTACHMENT_CONTEXT_MAX_BYTES", "41943040")
    monkeypatch.setenv("RUN_EXECUTION_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("RUNTIME_RECURSION_LIMIT", "500")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", "300")
    monkeypatch.setenv("AGENT_DEFAULT_MAX_OUTPUT_TOKENS", "8192")
    config = BaseConfig(_env_file=None)
    assert config.ARTIFACT_UPLOAD_MAX_BYTES == 30 * 1024 * 1024
    assert config.ATTACHMENT_CONTEXT_MAX_BYTES == 40 * 1024 * 1024
    assert "RUN_EXECUTION_TIMEOUT_SECONDS" not in BaseConfig.model_fields
    assert config.RUNTIME_RECURSION_LIMIT == 500
    assert config.MCP_CALL_TIMEOUT_SECONDS == 300
    assert config.AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS == 300
    assert config.AGENT_DEFAULT_MAX_OUTPUT_TOKENS == 8192
    for field in (
        "ARTIFACT_UPLOAD_MAX_BYTES",
        "ATTACHMENT_CONTEXT_MAX_BYTES",
        "RUNTIME_RECURSION_LIMIT",
        "MCP_CONNECT_TIMEOUT_SECONDS",
        "MCP_DISCOVERY_TIMEOUT_SECONDS",
        "MCP_CALL_TIMEOUT_SECONDS",
        "KNOWLEDGE_SEARCH_TIMEOUT_SECONDS",
        "KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS",
        "SCRIPT_EXECUTION_TIMEOUT_SECONDS",
        "SCRIPT_TEST_TIMEOUT_SECONDS",
    ):
        with pytest.raises(ValidationError):
            BaseConfig(
                _env_file=None,
                **{
                    field: 0,
                },
            )
    for field in ("MODEL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS", "MAX_OUTPUT_TOKENS"):
        maximum = "AGENT_MAX_OUTPUT_TOKENS" if field == "MAX_OUTPUT_TOKENS" else f"AGENT_MAX_{field}"
        with pytest.raises(ValidationError, match="不能超过"):
            BaseConfig(
                _env_file=None,
                **{
                    f"AGENT_DEFAULT_{field}": 999,
                    maximum: 998,
                },
            )


def test_dynamic_agent_run_and_provider_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建、更新与独立运行采用同一默认值和动态上限。"""
    config = get_config()
    monkeypatch.setattr(config, "AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", 300)
    monkeypatch.setattr(config, "AGENT_MAX_MODEL_TIMEOUT_SECONDS", 900)
    monkeypatch.setattr(config, "AGENT_DEFAULT_TOOL_TIMEOUT_SECONDS", 240)
    monkeypatch.setattr(config, "AGENT_MAX_TOOL_TIMEOUT_SECONDS", 600)
    monkeypatch.setattr(config, "AGENT_DEFAULT_MAX_OUTPUT_TOKENS", 8192)
    monkeypatch.setattr(config, "AGENT_MAX_OUTPUT_TOKENS", 32768)
    agent = AgentCreateIn(name="配置验证助手", default_model_id=1)
    assert (agent.model_timeout_seconds, agent.tool_timeout_seconds, agent.max_output_tokens) == (300, 240, 8192)
    standalone = RunCreateIn(model_id=1, input_text="测试默认值")
    assert (standalone.model_timeout_seconds, standalone.tool_timeout_seconds) == (300, 240)
    explicit = AgentCreateIn(name="明确指定", default_model_id=1, model_timeout_seconds=800, max_output_tokens=16384)
    assert explicit.model_timeout_seconds == 800 and explicit.max_output_tokens == 16384
    assert ProviderUpdateIn(timeout_seconds=900).timeout_seconds == 900
    assert AgentUpdateIn(expected_version=1).model_dump(exclude_unset=True) == {
        "expected_version": 1,
    }
    for field, value in (("model_timeout_seconds", 901), ("tool_timeout_seconds", 601), ("max_output_tokens", 32769)):
        with pytest.raises(ValidationError):
            AgentCreateIn(
                name="越界值",
                default_model_id=1,
                **{
                    field: value,
                },
            )
        with pytest.raises(ValidationError):
            AgentUpdateIn(
                expected_version=1,
                **{
                    field: value,
                },
            )
    with pytest.raises(ValidationError):
        RunCreateIn(model_id=1, input_text="越界值", model_timeout_seconds=901)
    with pytest.raises(ValidationError):
        ProviderUpdateIn(timeout_seconds=901)


def test_authenticated_limits_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """安全接口返回部署的业务配置，且不泄露密钥等完整配置。"""
    config = get_config()
    monkeypatch.setattr(config, "ARTIFACT_UPLOAD_MAX_BYTES", 30 * 1024 * 1024)
    monkeypatch.setattr(config, "ATTACHMENT_CONTEXT_MAX_BYTES", 40 * 1024 * 1024)
    monkeypatch.setattr(config, "RUNTIME_RECURSION_LIMIT", 500)
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 300)
    monkeypatch.setattr(config, "AGENT_DEFAULT_MAX_OUTPUT_TOKENS", 8192)

    async def current_admin() -> Admin:
        """只为隔离接口测试提供用户身份，不接触业务数据库。"""
        return Admin(id=1, username="limits-user")

    async def exercise() -> None:
        """检查未认证阻断以及精确的安全响应字段。"""
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/api/v1/agents/runtime-limits")).status_code in {401, 403}
            app.dependency_overrides[get_current_admin] = current_admin
            try:
                response = await client.get("/api/v1/agents/runtime-limits")
                assert response.status_code == 200, response.text
                data = response.json()["data"]
                assert data["upload_max_bytes"] == 30 * 1024 * 1024
                assert data["context_max_bytes"] == 40 * 1024 * 1024
                assert "execution_timeout_seconds" not in data
                assert data["recursion_limit"] == 500
                assert data["mcp_call_timeout_seconds"] == 300
                assert data["default_max_output_tokens"] == 8192
                assert set(data) == {
                    "upload_max_bytes",
                    "context_max_bytes",
                    "max_files",
                    "recursion_limit",
                    "mcp_connect_timeout_seconds",
                    "mcp_discovery_timeout_seconds",
                    "mcp_call_timeout_seconds",
                    "knowledge_search_timeout_seconds",
                    "knowledge_embedding_timeout_seconds",
                    "script_execution_timeout_seconds",
                    "script_test_timeout_seconds",
                    "default_model_timeout_seconds",
                    "max_model_timeout_seconds",
                    "default_tool_timeout_seconds",
                    "max_tool_timeout_seconds",
                    "default_max_output_tokens",
                    "max_output_tokens",
                }
            finally:
                app.dependency_overrides.pop(get_current_admin, None)

    asyncio.run(exercise())


def test_upload_and_context_use_live_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非默认的小容量覆盖上传、提取与 worker 累计正文及元数据检查。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_UPLOAD_MAX_BYTES", 1024)
    monkeypatch.setattr(get_config(), "ATTACHMENT_CONTEXT_MAX_BYTES", 1024)
    monkeypatch.setattr(message_attachments, "artifact_root", lambda: tmp_path)

    async def exercise() -> None:
        """使用真实临时 Blob 验证边界，避免只验证提示文字。"""
        first, _ = await save_upload(UploadFile(file=io.BytesIO(b"x" * 600), filename="first.txt"), 1, tmp_path)
        second, _ = await save_upload(UploadFile(file=io.BytesIO(b"x" * 600), filename="second.txt"), 1, tmp_path)
        assert "x" * 600 in await message_attachments.build_attachment_context([first])
        with pytest.raises(message_attachments.AttachmentContextUnavailable):
            await message_attachments.build_attachment_context([first, second])
        first_context = await message_attachments.build_attachment_context([first])
        second_context = await message_attachments.build_attachment_context([second])
        serialized_bytes = len(first_context.encode()) + len(second_context.encode())
        monkeypatch.setattr(get_config(), "ATTACHMENT_CONTEXT_MAX_BYTES", serialized_bytes)
        with pytest.raises(message_attachments.AttachmentContextUnavailable):
            await message_attachments.build_attachment_context([first, second])
        monkeypatch.setattr(get_config(), "ATTACHMENT_CONTEXT_MAX_BYTES", serialized_bytes + 1)
        assert len((await message_attachments.build_attachment_context([first, second])).encode()) == serialized_bytes + 1
        monkeypatch.setattr(get_config(), "ATTACHMENT_CONTEXT_MAX_BYTES", 1024)
        with pytest.raises(ArtifactValidationError):
            await save_upload(UploadFile(file=io.BytesIO(b"x" * 1025), filename="overflow.txt"), 1, tmp_path)
        assert extract_context_text(b"x" * 1024, "text/plain") == "x" * 1024
        with pytest.raises(ValueError):
            extract_context_text(b"x" * 1025, "text/plain")
        # 显式指定的知识库上传限额独立于对话配置。
        independent, _ = await save_upload(UploadFile(file=io.BytesIO(b"x" * 1025), filename="knowledge.txt"), 1, tmp_path, max_bytes=2048)
        assert independent.byte_size == 1025

    asyncio.run(exercise())


def test_multipart_limit_follows_upload_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置改变后同步限制声明长度及流式请求，保留知识库和 Skill 的独立预算。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_UPLOAD_MAX_BYTES", 1024)
    limit = 5 * 1024 + 1024 * 1024

    async def downstream(scope, receive, send) -> None:
        """读取完整请求后才发送响应，以复验无 Content-Length 的边界。"""
        while True:
            body = await receive()
            if not body.get("more_body"):
                break
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"",
            }
        )

    async def streamed():
        """分块发送超限数据，避免依赖请求声明长度。"""
        yield b"x" * limit
        yield b"x"

    async def exercise() -> None:
        """通过真实 ASGI 中间件检查请求总容量。"""
        middleware = ArtifactUploadLimitMiddleware(downstream)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=middleware), base_url="http://test") as client:
            assert (await client.post("/api/v1/artifacts/uploads", content=b"x" * limit)).status_code == 204
            assert (await client.post("/api/v1/artifacts/uploads", content=b"x" * (limit + 1))).status_code == 413
            assert (await client.post("/api/v1/artifacts/uploads", content=streamed())).status_code == 413
            assert (await client.post("/api/v1/knowledge-bases/1/documents", content=b"x" * (limit + 1))).status_code == 204
            assert (await client.post("/api/v1/skills/1/templates", content=b"x" * (limit + 1))).status_code == 204

    asyncio.run(exercise())


def test_run_and_orm_defaults_preserve_existing_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """新对象使用配置默认值，已保存 Agent 参数和 Run 快照不被环境变化覆盖。"""
    config = get_config()
    monkeypatch.setattr(config, "AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS", 300)
    monkeypatch.setattr(config, "AGENT_DEFAULT_TOOL_TIMEOUT_SECONDS", 240)
    monkeypatch.setattr(config, "AGENT_DEFAULT_MAX_OUTPUT_TOKENS", 8192)

    async def exercise() -> None:
        """在临时数据库复核直接 ORM 创建、服务创建及已保存值。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="limits-user", password="not-used"))
                agent = AgentConfig(owner_id=1, name="默认参数", default_model_id=1)
                existing = AgentConfig(owner_id=1, name="已存参数", default_model_id=1, max_output_tokens=2048, model_timeout_seconds=60)
                db.add_all([agent, existing])
                await db.flush()
                assert (agent.max_output_tokens, agent.model_timeout_seconds, agent.tool_timeout_seconds) == (8192, 300, 240)
                run = await create_run(db, 1, input_text="默认运行参数")
                assert (run.agent_max_output_tokens, run.model_timeout_seconds, run.tool_timeout_seconds) == (8192, 300, 240)
                snapshot_id, existing_id = run.id, existing.id
            monkeypatch.setattr(config, "AGENT_DEFAULT_MAX_OUTPUT_TOKENS", 16384)
            async with sessions() as db:
                old_run = await db.get(type(run), snapshot_id)
                old_agent = await db.get(AgentConfig, existing_id)
                assert old_run.agent_max_output_tokens == 8192 and old_run.model_timeout_seconds == 300
                assert old_agent.max_output_tokens == 2048 and old_agent.model_timeout_seconds == 60
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_runtime_default_migration_preserves_rows(tmp_path: Path) -> None:
    """从 046 升降级时仅改变列默认值，保留 Agent 参数与 Run 快照。"""
    import os
    import sqlite3
    import subprocess
    import sys

    database = tmp_path / "runtime-default-migration.db"
    env = os.environ.copy()
    env.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        }
    )
    root = Path(__file__).resolve().parents[1]

    def migrate(action: str, revision: str) -> None:
        """仅迁移测试数据库，不读取或改写本地业务库。"""
        subprocess.run([sys.executable, "-m", "alembic", action, revision], cwd=root, env=env, capture_output=True, check=True, text=True)

    async def seed() -> tuple[int, str]:
        """在旧版真实表结构中写入一组明确的历史参数。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="migration-user", password="not-used"))
                agent = AgentConfig(
                    owner_id=1,
                    name="历史配置",
                    default_model_id=1,
                    max_output_tokens=2048,
                    model_timeout_seconds=60,
                    tool_timeout_seconds=30,
                )
                db.add(agent)
                run = await create_run(
                    db, 1, input_text="保留快照", model_timeout_seconds=75, tool_timeout_seconds=45, agent_max_output_tokens=3072
                )
                return agent.id, run.id
        finally:
            await engine.dispose()

    migrate("upgrade", "head")
    agent_id, run_id = asyncio.run(seed())
    migrate("downgrade", "046")
    for action, revision, expected_default in (("upgrade", "head", None), ("downgrade", "046", "'120'"), ("upgrade", "head", None)):
        migrate(action, revision)
        with sqlite3.connect(database) as connection:
            if revision == "head":
                assert connection.execute("SELECT thinking_mode FROM yp_agent_config WHERE id=?", (agent_id,)).fetchone() == ("auto",)
                assert connection.execute("SELECT agent_thinking_mode FROM yp_agent_run WHERE id=?", (run_id,)).fetchone() == ("auto",)
            for table in ("yp_agent_config", "yp_agent_run"):
                columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
                assert {column[4] for column in columns if column[1] in {"model_timeout_seconds", "tool_timeout_seconds"}} == {
                    expected_default
                }
            assert connection.execute(
                "SELECT max_output_tokens, model_timeout_seconds, tool_timeout_seconds FROM yp_agent_config WHERE id=?", (agent_id,)
            ).fetchone() == (2048, 60, 30)
            assert connection.execute(
                "SELECT agent_max_output_tokens, model_timeout_seconds, tool_timeout_seconds FROM yp_agent_run WHERE id=?", (run_id,)
            ).fetchone() == (3072, 75, 45)
