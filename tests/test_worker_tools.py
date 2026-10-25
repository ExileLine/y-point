# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:29:06
# @Author  : yangyuexiong
# @File    : test_worker_tools.py

"""验证正式 worker 文件工具在调用当下重新检查授权。"""

import asyncio
import io
import json
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.base import Base, shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services import worker_tools as worker_tools_module
from app.services.artifact_store import save_upload
from app.services.run_manager import claim_run, create_run, request_cancel
from app.services.worker_tools import make_worker_tool_resolver
from app.services.tool_registry import ToolRegistry, ToolSpec


def test_text_artifact_tool_rechecks_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """工具只读取当前有效用户的小型文本文件。"""

    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker-tools.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "private-artifacts"
        root.mkdir(mode=0o700)
        monkeypatch.setattr(worker_tools_module, "artifact_root", lambda: root)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="tool-owner", password="not-used", role="user", status=1, is_deleted=0))
                db.add(Admin(id=2, username="other-owner", password="not-used", role="user", status=1, is_deleted=0))
                db.add(ProviderConnection(
                    id=1, name="test", name_key="test", kind="ollama", base_url="http://localhost:11434/v1",
                    creator_id=1, status=1, is_deleted=0,
                ))
                db.add(ModelCatalog(
                    id=1, provider_id=1, external_id="tool-model", display_name="tool-model", creator_id=1,
                    verified_capabilities=["tools"], status=1, is_deleted=0,
                ))
                owned, _ = await save_upload(UploadFile(file=io.BytesIO(b"owned text"), filename="owned.txt"), 1, root)
                foreign, _ = await save_upload(UploadFile(file=io.BytesIO(b"foreign text"), filename="foreign.txt"), 2, root)
                large, _ = await save_upload(UploadFile(file=io.BytesIO(b"x" * 65537), filename="large.txt"), 1, root)
                db.add_all([owned, foreign, large])
                plain = await create_run(db, 1)
                enabled = await create_run(db, 1, model_id=1, enable_text_artifact_tool=True)
                await claim_run(db, plain.id)
                await claim_run(db, enabled.id)
            resolver = make_worker_tool_resolver(sessions)
            async with sessions() as db:
                assert await resolver(db, plain) == ([], {})
                tools, interrupt_on = await resolver(db, enabled)
            assert interrupt_on == {"read_text_artifact": True}
            assert {item.name for item in tools} == {"list_text_artifacts", "read_text_artifact"}
            list_tool, read_tool = (next(item for item in tools if item.name == name) for name in (
                "list_text_artifacts", "read_text_artifact",
            ))
            assert json.loads(await list_tool.ainvoke({})) == [{
                "artifact_id": owned.id, "filename": "owned.txt", "byte_size": 10,
            }]
            result = json.loads(await read_tool.ainvoke({"artifact_id": owned.id}))
            assert result == {"filename": "owned.txt", "content": "owned text"}
            for artifact_id in (foreign.id, large.id):
                with pytest.raises(ValueError, match="文件不可用"):
                    await read_tool.ainvoke({"artifact_id": artifact_id})
            async with sessions.begin() as db:
                item = await db.get(type(owned), owned.id)
                item.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            with pytest.raises(ValueError, match="文件不可用"):
                await read_tool.ainvoke({"artifact_id": owned.id})
            assert json.loads(await list_tool.ainvoke({})) == []
            async with sessions.begin() as db:
                item = await db.get(type(owned), owned.id)
                item.expires_at = shanghai_now(naive=True) + timedelta(days=1)
                owner = await db.get(Admin, 1)
                owner.status = 0
            with pytest.raises(ValueError, match="工具授权已失效"):
                await read_tool.ainvoke({"artifact_id": owned.id})
            with pytest.raises(ValueError, match="工具授权已失效"):
                await list_tool.ainvoke({})
            async with sessions.begin() as db:
                provider = await db.get(ProviderConnection, 1)
                provider.status = 0
            with pytest.raises(ValueError, match="工具授权已失效"):
                await read_tool.ainvoke({"artifact_id": owned.id})
            with pytest.raises(ValueError, match="工具授权已失效"):
                await list_tool.ainvoke({})
            async with sessions.begin() as db:
                provider = await db.get(ProviderConnection, 1)
                provider.status = 1
            async with sessions.begin() as db:
                owner = await db.get(Admin, 1)
                owner.status = 1
                await request_cancel(db, enabled.id, 1)
            with pytest.raises(ValueError, match="工具授权已失效"):
                await list_tool.ainvoke({})
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_tool_registry_rejects_unsafe_declarations() -> None:
    """写工具必须声明审批与可核对的恢复策略。"""
    registry = ToolRegistry()

    def factory(context):
        return None

    def enabled(run):
        return True

    registry.register(ToolSpec("owned_read", factory, enabled, False, "read", "safe"))
    with pytest.raises(ValueError, match="重复"):
        registry.register(ToolSpec("owned_read", factory, enabled, False, "read", "safe"))
    with pytest.raises(ValueError, match="冲突"):
        registry.register(ToolSpec("read_file", factory, enabled, False, "read", "safe"))
    with pytest.raises(ValueError, match="写工具"):
        registry.register(ToolSpec("write_external", factory, enabled, False, "write", "idempotent"))
    with pytest.raises(ValueError, match="写工具"):
        registry.register(ToolSpec("write_external", factory, enabled, True, "write", "safe"))
