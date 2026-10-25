# -*- coding: utf-8 -*-
# @Time    : 2026-10-25 13:40:00
# @Author  : yangyuexiong
# @File    : artifact_migration.py

"""将已有私有本地文件逐个核对并复制到 S3 兼容后端。"""

import hashlib
from pathlib import Path

from botocore.exceptions import ClientError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.services.artifact_store import MAX_FILE_BYTES, ArtifactValidationError, storage_path
from app.services.private_blob_store import delete_blob, read_blob, store_blob, using_s3


def _matches(content: bytes, artifact: Artifact) -> bool:
    return len(content) == artifact.byte_size and hashlib.sha256(content).hexdigest() == artifact.sha256


async def _read_remote(root: Path, artifact: Artifact) -> bytes | None:
    try:
        return await read_blob(root, artifact.storage_key, max_bytes=MAX_FILE_BYTES)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound", "404"}:
            return None
        raise


async def migrate_local_artifacts_to_s3(
    sessions: async_sessionmaker[AsyncSession], root: Path, *, batch_size: int = 100,
) -> tuple[int, int]:
    """保留本地正文，远端已存在时只验证；首次上传后再次读取核对。"""
    if not using_s3():
        raise ValueError("迁移前必须启用 S3 存储后端")
    if not 1 <= batch_size <= 1000:
        raise ValueError("无效的迁移批量")
    uploaded = verified = 0
    last_id = ""
    while True:
        async with sessions() as db:
            rows = (await db.scalars(select(Artifact).where(
                Artifact.id > last_id, Artifact.state == "ready", Artifact.is_deleted == 0,
                or_(Artifact.expires_at.is_(None), Artifact.expires_at > shanghai_now(naive=True)),
            ).order_by(Artifact.id).limit(batch_size))).all()
        if not rows:
            return uploaded, verified
        for artifact in rows:
            remote = await _read_remote(root, artifact)
            if remote is not None:
                if not _matches(remote, artifact):
                    raise ArtifactValidationError(f"远端文件摘要不一致：{artifact.id}")
                verified += 1
                continue
            path = storage_path(root, artifact.storage_key)
            if path.is_symlink() or not path.is_file():
                raise ArtifactValidationError(f"本地文件不存在：{artifact.id}")
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            if not _matches(content, artifact):
                raise ArtifactValidationError(f"本地文件摘要不一致：{artifact.id}")
            await store_blob(root, artifact.storage_key, path)
            try:
                copied = await _read_remote(root, artifact)
                if copied is None or not _matches(copied, artifact):
                    raise ArtifactValidationError(f"远端上传校验失败：{artifact.id}")
            except BaseException:
                await delete_blob(root, artifact.storage_key)
                raise
            uploaded += 1
        last_id = rows[-1].id
