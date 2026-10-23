# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:30:51
# @Author  : yangyuexiong
# @File    : test_artifacts.py

"""验证私有文件上传、校验、隔离与授权下载。"""

from io import BytesIO
import os
import time
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import artifacts as artifact_api
from app.core.password import hash_password
from app.core.middleware import ArtifactUploadLimitMiddleware
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import Base, shanghai_now
from app.services.artifact_store import ArtifactValidationError, prune_artifacts, publish_generated_artifact
from app.services.run_manager import claim_run, complete_run, create_run


def office_bytes(kind: str) -> bytes:
    """构造只含必需结构的测试 Office 容器。"""
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml" if kind == "docx" else "xl/workbook.xml", "<root/>")
    return buffer.getvalue()


async def exercise_artifacts(database: Path, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """经真实 HTTP 路由验证批量上传与跨用户隔离。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as db:
        db.add_all([
            Admin(username="file-alice", mail="file-alice@example.com", password=hash_password("StrongPassword123"), role="user"),
            Admin(username="file-bob", mail="file-bob@example.com", password=hash_password("StrongPassword123"), role="user"),
        ])

    async def test_session():
        """让 HTTP 测试使用隔离业务库。"""
        async with sessions() as db:
            yield db

    monkeypatch.setattr(artifact_api, "artifact_root", lambda: root)
    root.mkdir(mode=0o700)
    app.dependency_overrides[get_db_session] = test_session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def login(username: str) -> dict[str, str]:
                """返回指定用户的 Bearer 请求头。"""
                response = await client.post("/api/v1/auth/login", json={"identifier": username, "password": "StrongPassword123"})
                assert response.status_code == 200, response.text
                return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}

            alice = await login("file-alice")
            bob = await login("file-bob")
            payload = [
                ("files", ("notes.txt", "第一份记录".encode(), "text/plain")),
                ("files", ("report.docx", office_bytes("docx"), "application/octet-stream")),
                ("files", ("table.xlsx", office_bytes("xlsx"), "application/octet-stream")),
            ]
            assert (await client.post("/api/v1/artifacts/uploads", files=payload)).status_code == 401
            created = await client.post("/api/v1/artifacts/uploads", headers=alice, files=payload)
            assert created.status_code == 201, created.text
            artifacts = created.json()["data"]
            assert len(artifacts) == 3 and all("storage_key" not in item and "owner_id" not in item for item in artifacts)
            for item, (_, (_, content, _)) in zip(artifacts, payload, strict=True):
                artifact_id = item["id"]
                assert (await client.get(f"/api/v1/artifacts/{artifact_id}", headers=bob)).status_code == 404
                assert (await client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=bob)).status_code == 404
                assert (await client.get(f"/api/v1/artifacts/{artifact_id}", headers=alice)).status_code == 200
                downloaded = await client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=alice)
                assert downloaded.status_code == 200 and downloaded.content == content
                assert downloaded.headers["x-content-type-options"] == "nosniff"
            invalid = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                ("files", ("valid.txt", b"safe", "text/plain")),
                ("files", ("fake.pdf", b"not a pdf", "application/pdf")),
            ])
            assert invalid.status_code == 400
            disguised = await client.post("/api/v1/artifacts/uploads", headers=alice, files={
                "files": ("fake.docx", office_bytes("xlsx"), "application/octet-stream"),
            })
            assert disguised.status_code == 400
            too_many = await client.post("/api/v1/artifacts/uploads", headers=alice, files=[
                ("files", (f"{index}.txt", b"ok", "text/plain")) for index in range(6)
            ])
            assert too_many.status_code == 400
            oversized = await client.post("/api/v1/artifacts/uploads", headers=alice, files={
                "files": ("large.txt", b"a" * (10 * 1024 * 1024 + 1), "text/plain"),
            })
            assert oversized.status_code in {400, 413}
            rejected_early = await client.post(
                "/api/v1/artifacts/uploads", headers={**alice, "Content-Length": str(52 * 1024 * 1024)},
                content=b"small",
            )
            assert rejected_early.status_code == 413
            first_id = artifacts[0]["id"]
            assert (await client.delete(f"/api/v1/artifacts/{first_id}", headers=bob)).status_code == 404
            assert (await client.delete(f"/api/v1/artifacts/{first_id}", headers=alice)).status_code == 200
            assert (await client.get(f"/api/v1/artifacts/{first_id}/download", headers=alice)).status_code == 404
            async with sessions() as db:
                rows = (await db.scalars(select(Artifact))).all()
                assert len(rows) == 3
                assert {item.storage_key for item in rows if item.state == "ready"} == {path.name for path in root.iterdir()}

            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="制作汇总")
                claimed = await claim_run(db, run.id)
                await complete_run(db, run.id, claimed.lease_token, checkpoint_id="complete", final_text="汇总完成")
            generated = await publish_generated_artifact(sessions, run.id, "summary-v1", "summary.txt", b"summary", root)
            repeated = await publish_generated_artifact(sessions, run.id, "summary-v1", "summary.txt", b"summary", root)
            assert repeated.id == generated.id and generated.source == "generated"
            with pytest.raises(ArtifactValidationError):
                await publish_generated_artifact(sessions, run.id, "summary-v1", "summary.txt", b"changed", root)
            listed = await client.get("/api/v1/artifacts", headers=alice, params={"run_id": run.id})
            assert listed.status_code == 200 and [item["id"] for item in listed.json()["data"]] == [generated.id]
            assert (await client.get("/api/v1/artifacts", headers=bob, params={"run_id": run.id})).status_code == 404

            async with sessions.begin() as db:
                stored = await db.get(Artifact, generated.id)
                stored.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            assert (await client.get(f"/api/v1/artifacts/{generated.id}/download", headers=alice)).status_code == 404
            orphan = root / ("a" * 32)
            orphan.write_bytes(b"orphan")
            os.utime(orphan, (time.time() - 7200, time.time() - 7200))
            fresh = root / ("b" * 32)
            fresh.write_bytes(b"fresh")
            expired_count, orphan_count = await prune_artifacts(sessions, root)
            assert expired_count == 1 and orphan_count == 1
            assert not orphan.exists() and fresh.exists() and not (root / generated.storage_key).exists()
            assert (await client.get("/api/v1/artifacts", headers=alice, params={"run_id": run.id})).json()["data"] == []
    finally:
        app.dependency_overrides.clear()
        await engine.dispose()


def test_artifact_upload_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """执行隔离的文件接口回归。"""
    import asyncio

    asyncio.run(exercise_artifacts(tmp_path / "artifacts.db", tmp_path / "private", monkeypatch))


async def exercise_stream_limit() -> None:
    """验证无 Content-Length 的分块请求仍受总量限制。"""
    chunks = iter([
        {"type": "http.request", "body": b"abc", "more_body": True},
        {"type": "http.request", "body": b"def", "more_body": False},
    ])
    sent = []

    async def receive() -> dict:
        """逐块提供模拟上传请求。"""
        return next(chunks)

    async def send(message: dict) -> None:
        """记录中间件返回的 ASGI 响应。"""
        sent.append(message)

    async def downstream(scope, limited_receive, send_response) -> None:
        """模拟在路由解析前读取全部请求体。"""
        await limited_receive()
        await limited_receive()

    middleware = ArtifactUploadLimitMiddleware(downstream, max_bytes=5)
    scope = {"type": "http", "method": "POST", "path": "/api/v1/artifacts/uploads", "headers": []}
    await middleware(scope, receive, send)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413


def test_chunked_upload_limit() -> None:
    """运行接收层超限回归。"""
    import asyncio

    asyncio.run(exercise_stream_limit())
