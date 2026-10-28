# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:48:04
# @Author  : yangyuexiong
# @File    : test_private_blob_store.py

"""验证私有 S3 兼容后端通过 Artifact Service 接口工作。"""

import asyncio
import hashlib
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import artifacts as artifact_api
from app.models.admin import Admin
from app.models.base import Base
from app.models.artifact import Artifact
from app.services import private_blob_store
from app.services import artifact_store
from app.services.artifact_migration import migrate_local_artifacts_to_s3
from app.services.artifact_store import prune_artifacts, publish_generated_artifact, save_upload
from app.services.run_manager import claim_run, complete_run, create_run


class MemoryS3:
    """只实现私有文件流程使用的 S3 方法。"""

    def __init__(self) -> None:
        """初始化内存对象、加密参数和分页请求记录。"""
        self.objects: dict[str, bytes] = {}
        self.encryption: list[str] = []
        self.list_calls: list[dict] = []

    def upload_file(self, path: str, bucket: str, key: str, *, ExtraArgs: dict) -> None:
        """将文件写入内存 S3，并记录服务端加密参数。"""
        assert bucket == "private-test" and key.startswith("y-point/artifacts/") and len(key) == 50
        self.encryption.append(ExtraArgs["ServerSideEncryption"])
        self.objects[key] = Path(path).read_bytes()

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        """从内存 S3 返回对象正文，缺失时模拟对象不存在错误。"""
        assert Bucket == "private-test"
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        """从内存 S3 删除对象，允许重复删除。"""
        assert Bucket == "private-test"
        self.objects.pop(Key, None)

    def list_objects_v2(self, *, Bucket: str, Prefix: str, MaxKeys: int, ContinuationToken: str | None = None) -> dict:
        """按前缀和继续标记分页返回内存 S3 的旧对象。"""
        assert Bucket == "private-test"
        assert Prefix == "y-point/artifacts/"
        self.list_calls.append({"Prefix": Prefix, "MaxKeys": MaxKeys, "ContinuationToken": ContinuationToken})
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        start = 0 if ContinuationToken is None else next((index for index, key in enumerate(keys) if key > ContinuationToken), len(keys))
        page = keys[start : start + MaxKeys]
        return {
            "Contents": [{"Key": key, "LastModified": old} for key in page],
            "IsTruncated": start + MaxKeys < len(keys),
            "NextContinuationToken": page[-1] if start + MaxKeys < len(keys) else None,
        }


def test_s3_artifact_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """对象上传、受控下载、产物发布和失联对象清理共用随机键。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：对象上传、受控下载、产物发布和失联对象清理共用随机键。"""
        fake = MemoryS3()
        monkeypatch.setattr(
            private_blob_store,
            "get_config",
            lambda: SimpleNamespace(
                ARTIFACT_STORAGE_BACKEND="s3",
                ARTIFACT_S3_BUCKET="private-test",
                ARTIFACT_S3_ENDPOINT_URL="",
                ARTIFACT_S3_REGION="",
                ARTIFACT_S3_ACCESS_KEY_ID="test",
                ARTIFACT_S3_SECRET_ACCESS_KEY="secret",
                ARTIFACT_S3_PREFIX="y-point/artifacts/",
            ),
        )
        monkeypatch.setattr(private_blob_store.boto3, "client", lambda *args, **kwargs: fake)
        root = tmp_path / "staging"
        root.mkdir(mode=0o700)
        monkeypatch.setattr(artifact_api, "artifact_root", lambda: root)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 's3-artifacts.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="s3-owner", password="not-used", role="user", status=1, is_deleted=0))
                uploaded, local = await save_upload(UploadFile(file=io.BytesIO(b"private input"), filename="input.txt"), 1, root)
                assert not local.exists() and fake.objects[f"y-point/artifacts/{uploaded.storage_key}"] == b"private input"
                db.add(uploaded)
                run = await create_run(db, 1, input_text="生成产物")
                claimed = await claim_run(db, run.id)
                await complete_run(db, run.id, claimed.lease_token, final_text="完成")
            async with sessions() as db:
                response = await artifact_api.download_artifact(uploaded.id, actor=SimpleNamespace(id=1), db=db)
                assert response.body == b"private input"
                assert response.headers["x-content-type-options"] == "nosniff"
            generated = await publish_generated_artifact(sessions, run.id, "export", "export.txt", b"export", root)
            assert fake.objects[f"y-point/artifacts/{generated.storage_key}"] == b"export"
            assert await private_blob_store.read_blob(root, generated.storage_key, max_bytes=6) == b"export"
            with pytest.raises(ValueError, match="读取限制"):
                await private_blob_store.read_blob(root, generated.storage_key, max_bytes=3)
            assert fake.encryption == ["AES256", "AES256"]
            with monkeypatch.context() as patcher:
                patcher.setattr(
                    artifact_store, "get_config", lambda: SimpleNamespace(ARTIFACT_REQUIRE_MALWARE_SCAN=False, ARTIFACT_RETENTION_DAYS=0)
                )
                before = dict(fake.objects)
                with pytest.raises(artifact_store.ArtifactValidationError, match="保留天数"):
                    await save_upload(UploadFile(file=io.BytesIO(b"invalid retention"), filename="bad.txt"), 1, root)
                assert fake.objects == before
                assert not list(root.iterdir())
            fake.objects[f"y-point/artifacts/{'a' * 32}"] = b"orphan"
            fake.objects["other-service/" + "b" * 32] = b"keep"
            expired, orphaned = await prune_artifacts(sessions, root)
            assert expired == 0 and orphaned == 1 and f"y-point/artifacts/{'a' * 32}" not in fake.objects
            assert "other-service/" + "b" * 32 in fake.objects
            async with sessions() as db:
                await artifact_api.delete_artifact(uploaded.id, actor=SimpleNamespace(id=1), db=db)
            assert f"y-point/artifacts/{uploaded.storage_key}" not in fake.objects
            with monkeypatch.context() as patcher:

                def fail_delete(**_kwargs):
                    """模拟远端对象删除失败，以验证原始异常和清理补偿。"""
                    raise ClientError({"Error": {"Code": "ServiceUnavailable", "Message": "unavailable"}}, "DeleteObject")

                patcher.setattr(fake, "delete_object", fail_delete)
                async with sessions() as db:
                    await artifact_api.delete_artifact(generated.id, actor=SimpleNamespace(id=1), db=db)
                    assert (await db.get(Artifact, generated.id)).state == "deleted"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_s3_orphan_scan_paginates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """前一页只有被引用对象时仍继续寻找后续页的孤儿对象。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：前一页只有被引用对象时仍继续寻找后续页的孤儿对象。"""
        fake = MemoryS3()
        monkeypatch.setattr(
            private_blob_store,
            "get_config",
            lambda: SimpleNamespace(
                ARTIFACT_STORAGE_BACKEND="s3",
                ARTIFACT_S3_BUCKET="private-test",
                ARTIFACT_S3_PREFIX="y-point/artifacts/",
                ARTIFACT_S3_ENDPOINT_URL="",
                ARTIFACT_S3_REGION="",
                ARTIFACT_S3_ACCESS_KEY_ID="test",
                ARTIFACT_S3_SECRET_ACCESS_KEY="secret",
            ),
        )
        monkeypatch.setattr(private_blob_store.boto3, "client", lambda *args, **kwargs: fake)
        root = tmp_path / "staging"
        root.mkdir(mode=0o700)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'paginated.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                for index, key in enumerate(("0" * 32, "a" * 32), start=1):
                    db.add(
                        Artifact(
                            id=f"00000000-0000-4000-8000-{index:012d}",
                            owner_id=1,
                            filename="keep.txt",
                            media_type="text/plain",
                            byte_size=1,
                            sha256="0" * 64,
                            storage_key=key,
                            source="upload",
                            state="ready",
                            status=1,
                            is_deleted=0,
                        )
                    )
            fake.objects[f"y-point/artifacts/{'0' * 32}"] = b"first"
            fake.objects[f"y-point/artifacts/{'a' * 32}"] = b"second"
            fake.objects[f"y-point/artifacts/{'b' * 32}"] = b"third"
            fake.objects[f"y-point/artifacts/{'c' * 32}"] = b"fourth"
            fake.objects["other-service/" + "c" * 32] = b"untouched"
            expired, orphaned = await prune_artifacts(sessions, root, limit=2)
            assert (expired, orphaned) == (0, 2)
            assert len(fake.list_calls) == 2
            assert fake.list_calls[1]["ContinuationToken"] == f"y-point/artifacts/{'a' * 32}"
            assert {key[-32:] for key in fake.objects if key.startswith("y-point/artifacts/")} == {"0" * 32, "a" * 32}
            assert "other-service/" + "c" * 32 in fake.objects
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_migrate_local_artifacts_is_verified_and_repeatable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """摘要不一致时停止；修复后重跑跳过已验证的远端文件。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：摘要不一致时停止；修复后重跑跳过已验证的远端文件。"""
        fake = MemoryS3()
        monkeypatch.setattr(
            private_blob_store,
            "get_config",
            lambda: SimpleNamespace(
                ARTIFACT_STORAGE_BACKEND="s3",
                ARTIFACT_S3_BUCKET="private-test",
                ARTIFACT_S3_PREFIX="y-point/artifacts/",
                ARTIFACT_S3_ENDPOINT_URL="",
                ARTIFACT_S3_REGION="",
                ARTIFACT_S3_ACCESS_KEY_ID="test",
                ARTIFACT_S3_SECRET_ACCESS_KEY="secret",
            ),
        )
        monkeypatch.setattr(private_blob_store.boto3, "client", lambda *args, **kwargs: fake)
        root = tmp_path / "existing-files"
        root.mkdir(mode=0o700)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        first, second = "1" * 32, "2" * 32
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                for index, key in enumerate((first, second), start=1):
                    content = f"file-{index}".encode()
                    (root / key).write_bytes(content)
                    db.add(
                        Artifact(
                            id=f"00000000-0000-4000-8000-{index:012d}",
                            owner_id=1,
                            filename=f"{index}.txt",
                            media_type="text/plain",
                            byte_size=len(content),
                            sha256=hashlib.sha256(content).hexdigest(),
                            storage_key=key,
                            source="upload",
                            state="ready",
                            status=1,
                            is_deleted=0,
                        )
                    )
            (root / second).write_bytes(b"damaged")
            with pytest.raises(artifact_store.ArtifactValidationError, match="本地文件摘要不一致"):
                await migrate_local_artifacts_to_s3(sessions, root, batch_size=1)
            assert fake.objects == {f"y-point/artifacts/{first}": b"file-1"}
            (root / second).write_bytes(b"file-2")
            assert await migrate_local_artifacts_to_s3(sessions, root, batch_size=1) == (1, 1)
            assert await migrate_local_artifacts_to_s3(sessions, root, batch_size=1) == (0, 2)
            fake.objects[f"y-point/artifacts/{first}"] = b"damaged"
            with pytest.raises(artifact_store.ArtifactValidationError, match="远端文件摘要不一致"):
                await migrate_local_artifacts_to_s3(sessions, root, batch_size=1)
            assert (root / first).read_bytes() == b"file-1"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
