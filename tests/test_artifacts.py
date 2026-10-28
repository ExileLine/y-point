# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:30:51
# @Author  : yangyuexiong
# @File    : test_artifacts.py

"""验证私有文件上传、校验、隔离与授权下载。"""

import asyncio
from io import BytesIO
import os
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import artifacts as artifact_api
from app.services import artifact_store
from app.core.password import hash_password
from app.core.config import get_config
from app.core.middleware import ArtifactUploadLimitMiddleware
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.agent_run import AgentRun
from app.models.base import Base, shanghai_now
from app.services.artifact_store import ArtifactValidationError, prune_artifacts, publish_generated_artifact, publish_pending_answers
from app.services.run_manager import claim_run, complete_run, create_run, replay_events


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
        db.add_all(
            [
                Admin(username="file-alice", mail="file-alice@example.com", password=hash_password("StrongPassword123"), role="user"),
                Admin(username="file-bob", mail="file-bob@example.com", password=hash_password("StrongPassword123"), role="user"),
            ]
        )

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
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    artifact_store,
                    "get_config",
                    lambda: SimpleNamespace(
                        ARTIFACT_RETENTION_DAYS=30,
                        ARTIFACT_REQUIRE_MALWARE_SCAN=False,
                        ARTIFACT_OWNER_MAX_FILES=3,
                        ARTIFACT_OWNER_MAX_BYTES=1024 * 1024,
                    ),
                )
                quota_rejected = await client.post(
                    "/api/v1/artifacts/uploads", headers=alice, files={"files": ("over-quota.txt", b"new file", "text/plain")}
                )
                assert quota_rejected.status_code == 400
                async with sessions() as db:
                    stored = (await db.scalars(select(Artifact))).all()
                    assert {path.name for path in root.iterdir()} == {item.storage_key for item in stored}
            for item, (_, (_, content, _)) in zip(artifacts, payload, strict=True):
                artifact_id = item["id"]
                assert (await client.get(f"/api/v1/artifacts/{artifact_id}", headers=bob)).status_code == 404
                assert (await client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=bob)).status_code == 404
                assert (await client.get(f"/api/v1/artifacts/{artifact_id}", headers=alice)).status_code == 200
                downloaded = await client.get(f"/api/v1/artifacts/{artifact_id}/download", headers=alice)
                assert downloaded.status_code == 200 and downloaded.content == content
                assert downloaded.headers["x-content-type-options"] == "nosniff"
            invalid = await client.post(
                "/api/v1/artifacts/uploads",
                headers=alice,
                files=[("files", ("valid.txt", b"safe", "text/plain")), ("files", ("fake.pdf", b"not a pdf", "application/pdf"))],
            )
            assert invalid.status_code == 400
            with monkeypatch.context() as patcher:
                attempted = []
                original_delete = artifact_api.delete_blob

                async def flaky_delete(storage_root: Path, key: str) -> None:
                    """模拟首个文件清理失败，后续文件仍可继续清理。"""
                    attempted.append(key)
                    if len(attempted) == 1:
                        raise OSError("object store unavailable")
                    await original_delete(storage_root, key)

                patcher.setattr(artifact_api, "delete_blob", flaky_delete)
                rolled_back = await client.post(
                    "/api/v1/artifacts/uploads",
                    headers=alice,
                    files=[
                        ("files", ("first.txt", b"first", "text/plain")),
                        ("files", ("second.txt", b"second", "text/plain")),
                        ("files", ("fake.pdf", b"not a pdf", "application/pdf")),
                    ],
                )
                assert rolled_back.status_code == 400
                assert len(attempted) == 2
                async with sessions() as db:
                    stored = (await db.scalars(select(Artifact))).all()
                    assert {path.name for path in root.iterdir()} == {item.storage_key for item in stored}
            disguised = await client.post(
                "/api/v1/artifacts/uploads", headers=alice, files={"files": ("fake.docx", office_bytes("xlsx"), "application/octet-stream")}
            )
            assert disguised.status_code == 400
            binary_text = await client.post(
                "/api/v1/artifacts/uploads", headers=alice, files={"files": ("binary.txt", b"text\x00binary", "text/plain")}
            )
            assert binary_text.status_code == 400
            truncated_pdf = await client.post(
                "/api/v1/artifacts/uploads",
                headers=alice,
                files={"files": ("truncated.pdf", b"%PDF-1.7\nmissing trailer", "application/pdf")},
            )
            assert truncated_pdf.status_code == 400
            duplicate_office = BytesIO()
            with ZipFile(duplicate_office, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", "<root/>")
                archive.writestr("word/document.xml", "<root/>")
            duplicated = await client.post(
                "/api/v1/artifacts/uploads",
                headers=alice,
                files={"files": ("duplicate.docx", duplicate_office.getvalue(), "application/octet-stream")},
            )
            assert duplicated.status_code == 400
            too_many = await client.post(
                "/api/v1/artifacts/uploads", headers=alice, files=[("files", (f"{index}.txt", b"ok", "text/plain")) for index in range(6)]
            )
            assert too_many.status_code == 400
            oversized = await client.post(
                "/api/v1/artifacts/uploads", headers=alice, files={"files": ("large.txt", b"a" * (10 * 1024 * 1024 + 1), "text/plain")}
            )
            assert oversized.status_code in {400, 413}
            rejected_early = await client.post(
                "/api/v1/artifacts/uploads", headers={**alice, "Content-Length": str(52 * 1024 * 1024)}, content=b"small"
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
            generated = await publish_generated_artifact(
                sessions, run.id, "summary-v1", "summary.txt", b"summary", root, source_artifact_id=artifacts[1]["id"]
            )
            repeated = await publish_generated_artifact(
                sessions, run.id, "summary-v1", "summary.txt", b"summary", root, source_artifact_id=artifacts[1]["id"]
            )
            assert repeated.id == generated.id and generated.source == "generated"
            assert generated.source_artifact_id == artifacts[1]["id"]
            with pytest.raises(ArtifactValidationError):
                await publish_generated_artifact(
                    sessions, run.id, "summary-v1", "summary.txt", b"changed", root, source_artifact_id=artifacts[1]["id"]
                )
            with pytest.raises(ArtifactValidationError):
                await publish_generated_artifact(
                    sessions, run.id, "other", "other.txt", b"content", root, source_artifact_id="00000000-0000-4000-8000-000000000000"
                )
            async with sessions() as db:
                events = await replay_events(db, run.id, 1)
                assert [(event.event_type, event.artifact_id) for event in events if event.artifact_id] == [
                    ("artifact_published", generated.id)
                ]
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


def test_local_orphan_scan_passes_referenced_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前几个旧文件仍被引用时，有限删除批量也能找到后面的孤儿。"""
    import asyncio

    async def exercise() -> None:
        """在隔离环境执行异步场景：前几个旧文件仍被引用时，有限删除批量也能找到后面的孤儿。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'orphans.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "private"
        root.mkdir(mode=0o700)
        keys = [str(index) * 32 for index in range(1, 4)]
        referenced = [root / key for key in keys[:2]]
        orphan = root / keys[2]
        unrelated = root / "notes.tmp"
        temporary = root / ("." + "4" * 32 + ".tmp")
        paths = [referenced[0], unrelated, referenced[1], orphan, temporary]
        for path in paths:
            path.write_bytes(b"old")
            os.utime(path, (time.time() - 7200, time.time() - 7200))
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                for index, key in enumerate(keys[:2], start=1):
                    db.add(
                        Artifact(
                            id=f"00000000-0000-4000-8000-{index:012d}",
                            owner_id=1,
                            filename=f"{index}.txt",
                            media_type="text/plain",
                            byte_size=3,
                            sha256="0" * 64,
                            storage_key=key,
                            source="upload",
                            state="ready",
                            status=1,
                            is_deleted=0,
                        )
                    )

            original_iterdir = Path.iterdir

            def ordered_iterdir(path: Path):
                """固定目录扫描顺序以复现已引用文件阻塞孤儿清理的边界。"""
                return iter(paths) if path == root else original_iterdir(path)

            with monkeypatch.context() as patcher:
                patcher.setattr(Path, "iterdir", ordered_iterdir)
                assert await prune_artifacts(sessions, root, limit=1) == (0, 1)
                assert not orphan.exists() and all(path.exists() for path in referenced)
                assert unrelated.exists() and temporary.exists()
                assert await prune_artifacts(sessions, root, limit=1) == (0, 1)
                assert not temporary.exists() and unrelated.exists()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_MYSQL_M3") != "1", reason="需显式启用隔离 MySQL 配额验收")
@pytest.mark.parametrize("max_files,max_bytes", [(1, 1024), (10, 3)])
def test_mysql_concurrent_artifact_quota(monkeypatch: pytest.MonkeyPatch, max_files: int, max_bytes: int) -> None:
    """两个事务争抢最后一个文件或字节名额时，只允许一个提交。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：两个事务争抢最后一个文件或字节名额时，只允许一个提交。"""
        name = f"yp_m3_quota_{uuid4().hex[:12]}"
        base_url = make_url(get_config().mysql_async_url)
        admin_engine = create_async_engine(base_url.set(database=None), isolation_level="AUTOCOMMIT")
        engine = None
        created = False
        try:
            async with admin_engine.connect() as db:
                await db.execute(text(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4"))
            created = True
            engine = create_async_engine(base_url.set(database=name))
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with engine.begin() as db:
                await db.run_sync(lambda connection: Base.metadata.create_all(connection, tables=[Admin.__table__, Artifact.__table__]))
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="quota-owner", password="not-used", role="user", status=1, is_deleted=0))
            monkeypatch.setattr(
                artifact_store,
                "get_config",
                lambda: SimpleNamespace(ARTIFACT_OWNER_MAX_FILES=max_files, ARTIFACT_OWNER_MAX_BYTES=max_bytes),
            )
            first_ready = asyncio.Event()
            second_ready = asyncio.Event()
            release_first = asyncio.Event()

            async def upload(index: int) -> None:
                """在独立事务内上传测试文件以验证并发配额。"""
                async with sessions.begin() as db:
                    if index == 2:
                        second_ready.set()
                    await artifact_store.enforce_owner_quota(db, 1, add_count=1, add_bytes=3)
                    if index == 1:
                        first_ready.set()
                        await release_first.wait()
                    db.add(
                        Artifact(
                            id=str(uuid4()),
                            owner_id=1,
                            filename=f"{index}.txt",
                            media_type="text/plain",
                            byte_size=3,
                            sha256="0" * 64,
                            storage_key=uuid4().hex,
                            source="upload",
                            state="ready",
                            status=1,
                            is_deleted=0,
                        )
                    )

            first = asyncio.create_task(upload(1))
            await asyncio.wait_for(first_ready.wait(), timeout=5)
            second = asyncio.create_task(upload(2))
            await asyncio.wait_for(second_ready.wait(), timeout=5)
            try:
                await asyncio.sleep(0.1)
                assert not second.done()
            finally:
                release_first.set()
            await asyncio.wait_for(first, timeout=5)
            with pytest.raises(ArtifactValidationError, match="配额"):
                await asyncio.wait_for(second, timeout=5)
            async with sessions() as db:
                assert len((await db.scalars(select(Artifact))).all()) == 1
        finally:
            if engine is not None:
                await engine.dispose()
            if created:
                async with admin_engine.connect() as db:
                    await db.execute(text(f"DROP DATABASE `{name}`"))
            await admin_engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure",
    [
        ArtifactValidationError("temporary quota failure"),
        OSError("temporary disk failure"),
        S3UploadFailedError("temporary transfer failure"),
        ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "unavailable"}}, "PutObject"),
    ],
    ids=["validation", "local-storage", "s3-transfer", "s3-service"],
)
def test_answer_publication_failure_does_not_starve_later_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception) -> None:
    """失败 Run 退避后，下一轮有限批量仍能处理后续 Run。"""
    import asyncio

    async def exercise() -> None:
        """在隔离环境执行异步场景：失败 Run 退避后，下一轮有限批量仍能处理后续 Run。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'answers.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "private"
        root.mkdir(mode=0o700)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="answers-owner", password="not-used", role="user", status=1, is_deleted=0))
                first = await create_run(db, 1, input_text="first", publish_answer_artifact=True)
                claimed = await claim_run(db, first.id)
                await complete_run(db, first.id, claimed.lease_token, final_text="first answer")
                second = await create_run(db, 1, input_text="second", publish_answer_artifact=True)
                claimed = await claim_run(db, second.id)
                await complete_run(db, second.id, claimed.lease_token, final_text="second answer")
                await db.execute(
                    update(AgentRun).where(AgentRun.id == first.id).values(update_time=shanghai_now(naive=True) - timedelta(days=1))
                )
            attempts = []
            unavailable = True

            async def publish(_sessions, run_id, *_args, **_kwargs):
                """发布测试生成产物以验证幂等与清理行为。"""
                attempts.append(run_id)
                if unavailable and run_id == first.id:
                    raise failure

            monkeypatch.setattr(artifact_store, "publish_generated_artifact", publish)
            assert await publish_pending_answers(sessions, root, limit=1) == 0
            async with sessions() as db:
                failed = await db.get(AgentRun, first.id)
                assert failed.answer_artifact_retry_at > shanghai_now(naive=True)
            assert await publish_pending_answers(sessions, root, limit=1) == 1
            assert attempts == [first.id, second.id]
            unavailable = False
            async with sessions.begin() as db:
                await db.execute(
                    update(AgentRun)
                    .where(AgentRun.id == first.id)
                    .values(
                        answer_artifact_retry_at=shanghai_now(naive=True) - timedelta(seconds=1),
                        update_time=shanghai_now(naive=True) - timedelta(days=1),
                    )
                )
            assert await publish_pending_answers(sessions, root, limit=1) == 1
            assert attempts == [first.id, second.id, first.id]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_publication_cleanup_failure_preserves_validation_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """远端清理失败不能覆盖产物发布失败，且临时文件仍需清除。"""
    import asyncio

    async def exercise() -> None:
        """在隔离环境执行异步场景：远端清理失败不能覆盖产物发布失败，且临时文件仍需清除。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'publication.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "private"
        root.mkdir(mode=0o700)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="publication-owner", password="not-used", role="user", status=1, is_deleted=0))
                run = await create_run(db, 1, input_text="publish")
                claimed = await claim_run(db, run.id)
                await complete_run(db, run.id, claimed.lease_token, final_text="answer")

            attempts = []

            async def fake_store(_root: Path, key: str, _path: Path) -> None:
                """模拟成功写入远端对象以触发后续事务失败路径。"""
                attempts.append(("store", key))

            async def failed_delete(_root: Path, key: str) -> None:
                """模拟删除对象故障，验证清理继续执行并保留原始异常。"""
                attempts.append(("delete", key))
                raise OSError("object store unavailable")

            monkeypatch.setattr(artifact_store, "using_s3", lambda: True)
            monkeypatch.setattr(artifact_store, "store_blob", fake_store)
            monkeypatch.setattr(artifact_store, "delete_blob", failed_delete)
            monkeypatch.setattr(
                artifact_store, "get_config", lambda: SimpleNamespace(ARTIFACT_REQUIRE_MALWARE_SCAN=False, ARTIFACT_RETENTION_DAYS=0)
            )
            with pytest.raises(ArtifactValidationError, match="保留天数"):
                await publish_generated_artifact(sessions, run.id, "answer", "answer.txt", b"answer", root)
            assert [kind for kind, _ in attempts] == ["store", "delete"]
            assert attempts[0][1] == attempts[1][1]
            assert not list(root.iterdir())
            async with sessions() as db:
                assert (await db.scalars(select(Artifact))).all() == []
        finally:
            await engine.dispose()

    asyncio.run(exercise())


async def exercise_stream_limit() -> None:
    """验证无 Content-Length 的分块请求仍受总量限制。"""
    chunks = iter(
        [{"type": "http.request", "body": b"abc", "more_body": True}, {"type": "http.request", "body": b"def", "more_body": False}]
    )
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


def test_malware_scan_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """启用扫描后，扫描器不可用或报告感染都不能接受文件。"""
    import asyncio

    async def exercise() -> None:
        """在隔离环境执行异步场景：启用扫描后，扫描器不可用或报告感染都不能接受文件。"""
        path = tmp_path / "scan.txt"
        path.write_bytes(b"safe")
        monkeypatch.setattr(artifact_store, "get_config", lambda: SimpleNamespace(ARTIFACT_REQUIRE_MALWARE_SCAN=True))
        monkeypatch.setattr(artifact_store.shutil, "which", lambda name: None)
        with pytest.raises(ArtifactValidationError, match="扫描器不可用"):
            await artifact_store.scan_file(path)

        class FakeProcess:
            async def wait(self) -> int:
                """模拟扫描进程退出并返回发现恶意内容的状态码。"""
                return 1

        async def fake_scan(*args, **kwargs):
            """模拟文件扫描阶段的失败以验证上传回滚。"""
            return FakeProcess()

        monkeypatch.setattr(artifact_store.shutil, "which", lambda name: "/usr/bin/clamscan")
        monkeypatch.setattr(artifact_store.asyncio, "create_subprocess_exec", fake_scan)
        with pytest.raises(ArtifactValidationError, match="恶意内容"):
            await artifact_store.scan_file(path)

    asyncio.run(exercise())
