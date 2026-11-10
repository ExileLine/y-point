# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 15:53:41
# @Author  : yangyuexiong
# @File    : test_artifact_cleanup.py

"""通过隔离 HTTP、真实数据库及临时磁盘验证开发文件盘点与物理清理。"""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.enums import ArtifactCleanupResult, ArtifactOrigin, ArtifactState, MessageRole, RunState, UserRole
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.artifact import Artifact
from app.models.audit_event import AuditEvent
from app.models.base import Base, shanghai_now
from app.models.knowledge import KnowledgeDocumentVersion
from app.models.run_artifact_read import RunArtifactRead
from app.services import artifact_cleanup

BASE = "/api/v1/artifacts/dev-files"


@asynccontextmanager
async def cleanup_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """建立隔离用户、Agent、会话、历史文件及真实登录令牌，不访问本机业务库或正文。"""
    root = tmp_path / "artifacts"
    root.mkdir(mode=0o700)
    config = get_config()
    monkeypatch.setattr(config, "ENV", "development")
    monkeypatch.setattr(config, "ARTIFACT_STORAGE_BACKEND", "local")
    monkeypatch.setattr(config, "ARTIFACT_STORAGE_DIR", str(root))
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cleanup.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    previous_overrides = app.dependency_overrides.copy()
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions.begin() as db:
            password = hash_password("StrongPassword123")
            db.add_all(
                [
                    Admin(id=1, username="cleanup-alice", password=password, role=UserRole.ADMIN),
                    Admin(id=2, username="cleanup-bob", password=password, role=UserRole.ADMIN),
                    Admin(id=3, username="cleanup-user", password=password, role=UserRole.USER),
                ],
            )
            db.add_all([AgentConfig(id=i, owner_id=1 if i < 3 else 2, name=f"Agent {i}", default_model_id=1) for i in (1, 2, 3)])
            conversations = [
                AgentSession(id=str(uuid4()), owner_id=1 if i < 3 else 2, agent_id=1 if i < 2 else i, thread_id=str(uuid4()), title=f"S{i}") for i in range(4)
            ]
            db.add_all(conversations)
            runs = [
                AgentRun(
                    id=str(uuid4()),
                    owner_id=session.owner_id,
                    agent_id=session.agent_id,
                    session_id=session.id,
                    thread_id=session.thread_id,
                    state=RunState.COMPLETED,
                )
                for session in conversations
            ]
            failed_run = AgentRun(
                id=str(uuid4()),
                owner_id=1,
                agent_id=1,
                session_id=conversations[0].id,
                thread_id=conversations[0].thread_id,
                state=RunState.FAILED,
            )
            db.add_all([*runs, failed_run])
            messages = [AgentRunMessage(run_id=run.id, owner_id=run.owner_id, role=MessageRole.USER, content="测试文件") for run in runs]
            db.add_all(messages)
            await db.flush()
            files = {}

            def record(index: int, name: str, *, run=None, source=ArtifactOrigin.UPLOAD, owner=1, state=ArtifactState.READY):
                """创建可追踪的临时正文与元数据，用于实际路径删除及关联查询断言。"""
                content = f"cleanup fixture {index}".encode()
                file = Artifact(
                    id=str(uuid4()),
                    owner_id=owner,
                    run_id=run.id if run else None,
                    filename=name,
                    media_type="text/plain",
                    byte_size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    storage_key=f"{index:032x}",
                    source=source,
                    state=state,
                )
                db.add(file)
                (root / file.storage_key).write_bytes(content)
                files[index] = file
                return file

            record(1, "plan.txt")
            record(2, "report.txt", run=runs[0], source=ArtifactOrigin.GENERATED)
            deleted = record(3, "deleted.txt", run=runs[0], source=ArtifactOrigin.GENERATED, state=ArtifactState.DELETED)
            deleted.is_deleted = 1
            (root / deleted.storage_key).unlink()
            record(4, "unrelated.txt", run=runs[2], source=ArtifactOrigin.GENERATED)
            record(5, "other-user.txt", run=runs[3], source=ArtifactOrigin.GENERATED, owner=2)
            record(6, "template.txt", run=runs[0], source=ArtifactOrigin.SKILL_RESOURCE)
            record(7, "重复.txt")
            record(8, "重复.txt")
            record(9, "unused.txt")
            record(10, "knowledge.txt")
            record(11, "pending-failed.txt", run=failed_run, source=ArtifactOrigin.GENERATED, state=ArtifactState.PENDING)
            expired = record(12, "expired.txt", run=runs[0], source=ArtifactOrigin.GENERATED)
            expired.expires_at = shanghai_now(naive=True) - timedelta(days=1)
            db.add(
                KnowledgeDocumentVersion(
                    document_id=1,
                    version=1,
                    artifact_id=files[10].id,
                    filename=files[10].filename,
                    sha256=files[10].sha256,
                    media_type=files[10].media_type,
                    byte_size=files[10].byte_size,
                ),
            )
            for message_index, indices in ((0, (1, 7, 8)), (1, (1,))):
                for position, index in enumerate(indices):
                    file = files[index]
                    db.add(
                        AgentMessageAttachment(
                            message_id=messages[message_index].id,
                            artifact_id=file.id,
                            position=position,
                            filename=file.filename,
                            media_type=file.media_type,
                            byte_size=file.byte_size,
                            included_in_context=True,
                        ),
                    )

        async def isolated_session():
            """让认证和业务接口共用该次测试的隔离数据库会话。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db_session] = isolated_session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            headers = []
            for username in ("cleanup-alice", "cleanup-bob", "cleanup-user"):
                response = await client.post(
                    "/api/v1/auth/login",
                    json={
                        "identifier": username,
                        "password": "StrongPassword123",
                    },
                )
                assert response.status_code == 200, response.text
                headers.append(
                    {
                        "Authorization": f"Bearer {response.json()['data']['access_token']}",
                    },
                )
            yield SimpleNamespace(client=client, sessions=sessions, files=files, conversations=conversations, root=root, headers=headers)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()


def test_cleanup_inventory_scope_and_pagination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent 汇总去重，session 只返回自身引用，并包含已撤销、过期、待发布和磁盘缺失项。"""

    async def exercise() -> None:
        """经实际 HTTP 路由查询两个范围及分页，不改变磁盘或元数据。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            response = await lab.client.get(
                BASE,
                headers=lab.headers[0],
                params={
                    "agent_id": 1,
                    "page_size": 2,
                },
            )
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            data = response.json()["data"]
            assert data["total"] == 7 and len(data["items"]) == 2
            collected = []
            for page in range(1, 5):
                response = await lab.client.get(
                    BASE,
                    headers=lab.headers[0],
                    params={
                        "agent_id": 1,
                        "page": page,
                        "page_size": 2,
                    },
                )
                collected.extend(response.json()["data"]["items"])
            expected = {lab.files[i].id for i in (1, 2, 3, 7, 8, 11, 12)}
            assert {item["artifact_id"] for item in collected} == expected and len(collected) == 7
            by_id = {item["artifact_id"]: item for item in collected}
            for file in (lab.files[i] for i in (1, 2, 3, 7, 8, 11, 12)):
                assert by_id[file.id]["filename"] == file.filename and by_id[file.id]["storage_name"] == file.storage_key
                assert by_id[file.id]["sha256"] == file.sha256
                assert "storage_path" not in by_id[file.id]
            assert by_id[lab.files[3].id]["exists_on_disk"] is False
            assert by_id[lab.files[3].id]["is_deleted"] is True
            scoped = await lab.client.get(
                BASE,
                headers=lab.headers[0],
                params={
                    "session_id": lab.conversations[1].id,
                },
            )
            assert scoped.status_code == 200 and scoped.json()["data"]["total"] == 1
            assert scoped.json()["data"]["items"][0]["artifact_id"] == lab.files[1].id
            assert (lab.root / lab.files[2].storage_key).is_file()

    asyncio.run(exercise())


def test_cleanup_physical_delete_revoke_and_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """原名与随机名可删除真实临时文件，记录及引用保留，重复删除返回已缺失。"""

    async def exercise() -> None:
        """校验物理文件、授权下载、数据库状态与审计，而不只检查 HTTP 成功。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            keys = [lab.files[1].storage_key, lab.files[2].storage_key]
            response = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [keys[0], lab.files[1].filename, lab.files[2].filename],
                },
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert data["deleted_count"] == 2 and data["failed_count"] == 0
            assert [item["storage_name"] for item in data["items"]] == keys
            assert all(not (lab.root / key).exists() for key in keys)
            for index in (1, 2):
                response = await lab.client.get(f"/api/v1/artifacts/{lab.files[index].id}/download", headers=lab.headers[0])
                assert response.status_code == 404
            async with lab.sessions() as db:
                for index in (1, 2):
                    file = await db.get(Artifact, lab.files[index].id)
                    assert file.state == ArtifactState.DELETED and file.is_deleted == 1
                assert await db.scalar(select(func.count()).select_from(AgentMessageAttachment)) == 4
                assert await db.scalar(select(func.count()).select_from(AgentRunMessage)) == 4
                actions = list(await db.scalars(select(AuditEvent.action).where(AuditEvent.action.like("artifact.dev_cleanup_%"))))
                assert actions.count("artifact.dev_cleanup_requested") == 2 and actions.count("artifact.dev_cleanup_deleted") == 2
            repeated = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": keys,
                },
            )
            assert repeated.status_code == 200
            assert repeated.json()["data"]["already_missing_count"] == 2
            assert (lab.root / lab.files[5].storage_key).is_file()
            pending = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[11].storage_key],
                },
            )
            assert pending.status_code == 200 and pending.json()["data"]["deleted_count"] == 1
            unused = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[9].storage_key, lab.files[12].storage_key],
                },
            )
            assert unused.status_code == 200 and unused.json()["data"]["deleted_count"] == 2

    asyncio.run(exercise())


@pytest.mark.parametrize("target,expected", [("重复.txt", 409), ("other-user.txt", 404), ("template.txt", 409), ("knowledge.txt", 409), ("missing.txt", 404)])
def test_cleanup_preflight_prevents_partial_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, expected: int) -> None:
    """歧义、跨用户、不可变资源或未知名称在整批删除前拒绝，有效文件也不被先删。"""

    async def exercise() -> None:
        """同时提交合法目标及错误目标，确认磁盘和授权记录均未改变。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            response = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[2].storage_key, target],
                },
            )
            assert response.status_code == expected, response.text
            assert (lab.root / lab.files[2].storage_key).is_file()
            async with lab.sessions() as db:
                assert (await db.get(Artifact, lab.files[2].id)).state == ArtifactState.READY

    asyncio.run(exercise())


@pytest.mark.parametrize("state", [RunState.QUEUED, RunState.RUNNING, RunState.WAITING_FOR_APPROVAL, RunState.CANCEL_REQUESTED])
def test_cleanup_rejects_active_shared_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: RunState) -> None:
    """同一上传在多个会话复用时，其他关联会话的未结束运行也阻止删除。"""

    async def exercise() -> None:
        """创建关联会话的新运行，验证保护覆盖排队、审批和取消等待。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            session = lab.conversations[1]
            async with lab.sessions.begin() as db:
                db.add(AgentRun(id=str(uuid4()), owner_id=1, agent_id=1, session_id=session.id, thread_id=session.thread_id, state=state))
            response = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[1].storage_key],
                },
            )
            assert response.status_code == 409 and (lab.root / lab.files[1].storage_key).is_file()

    asyncio.run(exercise())


def test_cleanup_rejects_active_generated_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """旧产物所在会话的新运行尚未开始读取时，也不能清理其可能续读的文件。"""

    async def exercise() -> None:
        """验证在用保护覆盖尚无读取记录的新一轮任务。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            session = lab.conversations[0]
            async with lab.sessions.begin() as db:
                db.add(
                    AgentRun(
                        id=str(uuid4()),
                        owner_id=1,
                        agent_id=1,
                        session_id=session.id,
                        thread_id=session.thread_id,
                        state=RunState.QUEUED,
                    ),
                )
            response = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[2].storage_key],
                },
            )
            assert response.status_code == 409 and (lab.root / lab.files[2].storage_key).is_file()

    asyncio.run(exercise())


def test_cleanup_rejects_active_artifact_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式旧产物读取记录也参与在用保护，不依赖产物所属 Run 的终态。"""

    async def exercise() -> None:
        """构造仍在读取的 Run 关系，确认完成 Run 的旧产物不能被提前清理。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            run_id = str(uuid4())
            async with lab.sessions.begin() as db:
                db.add(AgentRun(id=run_id, owner_id=1, thread_id=str(uuid4()), state=RunState.RUNNING))
                db.add(RunArtifactRead(run_id=run_id, owner_id=1, source_artifact_id=lab.files[2].id))
            response = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[2].storage_key],
                },
            )
            assert response.status_code == 409 and (lab.root / lab.files[2].storage_key).is_file()

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_cleanup_refuses_non_regular_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    """不跟随链接、不递归目录，整批拒绝时也不删除旁边的正常文件。"""

    async def exercise() -> None:
        """用真实临时符号链接或目录验证磁盘边界。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            target = lab.root / lab.files[1].storage_key
            target.unlink()
            outside = tmp_path / "outside.txt"
            outside.write_text("OUTSIDE")
            if kind == "symlink":
                target.symlink_to(outside)
            else:
                target.mkdir()
            response = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[2].storage_key, lab.files[1].storage_key],
                },
            )
            assert response.status_code == 409
            assert outside.read_text() == "OUTSIDE" and (lab.root / lab.files[2].storage_key).is_file()

    asyncio.run(exercise())


def test_cleanup_reports_io_failure_and_allows_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """磁盘删除失败逐项返回，记录已撤销，其他文件继续清理，失败项可重试。"""

    async def exercise() -> None:
        """仅注入一个文件的 PermissionError，其余路径继续执行真实 unlink。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            keys = [lab.files[1].storage_key, lab.files[2].storage_key]
            original_unlink = artifact_cleanup._unlink_local_file

            def fail_one(path: Path) -> ArtifactCleanupResult:
                """模拟文件权限变化，同时保证成功项真的从临时目录移除。"""
                if path.name == keys[1]:
                    raise PermissionError("PRIVATE-FILESYSTEM-DETAIL")
                return original_unlink(path)

            with monkeypatch.context() as patcher:
                patcher.setattr(artifact_cleanup, "_unlink_local_file", fail_one)
                response = await lab.client.post(
                    BASE + "/delete",
                    headers=lab.headers[0],
                    json={
                        "filenames": keys,
                    },
                )
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["deleted_count"] == 1 and data["failed_count"] == 1
            assert "PRIVATE-FILESYSTEM-DETAIL" not in response.text
            assert not (lab.root / keys[0]).exists() and (lab.root / keys[1]).is_file()
            async with lab.sessions() as db:
                assert (await db.get(Artifact, lab.files[2].id)).state == ArtifactState.DELETED
            retried = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [keys[1]],
                },
            )
            assert retried.status_code == 200 and retried.json()["data"]["deleted_count"] == 1

    asyncio.run(exercise())


def test_cleanup_access_validation_and_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证登录、角色、环境、本地后端、归属、入参门禁及 OpenAPI 静态路由。"""

    async def exercise() -> None:
        """对两个真实 HTTP 入口检查拒绝路径，不操作任何真实开发文件。"""
        async with cleanup_client(tmp_path, monkeypatch) as lab:
            assert (await lab.client.get(BASE)).status_code == 401
            assert (
                await lab.client.get(
                    BASE,
                    headers=lab.headers[2],
                    params={
                        "agent_id": 1,
                    },
                )
            ).status_code == 403
            assert (
                await lab.client.post(
                    BASE + "/delete",
                    headers=lab.headers[2],
                    json={
                        "filenames": [lab.files[2].storage_key],
                    },
                )
            ).status_code == 403
            for params in (
                {},
                {
                    "agent_id": 1,
                    "session_id": lab.conversations[0].id,
                },
            ):
                assert (await lab.client.get(BASE, headers=lab.headers[0], params=params)).status_code == 400
            for params in (
                {
                    "agent_id": 3,
                },
                {
                    "session_id": lab.conversations[3].id,
                },
            ):
                assert (await lab.client.get(BASE, headers=lab.headers[0], params=params)).status_code == 404
            for filenames in ([], ["../outside.txt"], ["/tmp/outside.txt"], ["a\\b"], [""], ["   "], ["x", "x"], ["x"] * 101):
                response = await lab.client.post(
                    BASE + "/delete",
                    headers=lab.headers[0],
                    json={
                        "filenames": filenames,
                    },
                )
                assert response.status_code == 400, response.text
            extra = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [lab.files[2].storage_key],
                    "dry_run": True,
                },
            )
            assert extra.status_code == 400 and (lab.root / lab.files[2].storage_key).is_file()
            orphan = lab.root / ("f" * 32)
            orphan.write_text("unowned orphan")
            unknown = await lab.client.post(
                BASE + "/delete",
                headers=lab.headers[0],
                json={
                    "filenames": [orphan.name],
                },
            )
            assert unknown.status_code == 404 and orphan.is_file()
            config = get_config()
            for env in ("production", "staging", "test"):
                with monkeypatch.context() as patcher:
                    patcher.setattr(config, "ENV", env)
                    assert (
                        await lab.client.get(
                            BASE,
                            headers=lab.headers[0],
                            params={
                                "agent_id": 1,
                            },
                        )
                    ).status_code == 403
                    assert (
                        await lab.client.post(
                            BASE + "/delete",
                            headers=lab.headers[0],
                            json={
                                "filenames": [lab.files[2].storage_key],
                            },
                        )
                    ).status_code == 403
            with monkeypatch.context() as patcher:
                patcher.setattr(config, "ARTIFACT_STORAGE_BACKEND", "s3")
                assert (
                    await lab.client.get(
                        BASE,
                        headers=lab.headers[0],
                        params={
                            "agent_id": 1,
                        },
                    )
                ).status_code == 409
                assert (
                    await lab.client.post(
                        BASE + "/delete",
                        headers=lab.headers[0],
                        json={
                            "filenames": [lab.files[2].storage_key],
                        },
                    )
                ).status_code == 409
            schema = (await lab.client.get("/api/v1/openapi.json")).json()
            assert BASE in schema["paths"] and BASE + "/delete" in schema["paths"]
            assert (lab.root / lab.files[2].storage_key).is_file()

    asyncio.run(exercise())
