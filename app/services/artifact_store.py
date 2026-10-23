# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : artifact_store.py

"""管理受限上传及私有文件路径。"""

import hashlib
import os
import stat
import time
import zipfile
import zlib
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import BASE_DIR, get_config
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.artifact import Artifact
from app.models.base import shanghai_now

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
ORPHAN_GRACE_SECONDS = 3600
MEDIA_TYPES = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class ArtifactValidationError(ValueError):
    """上传文件不符合格式或大小限制。"""


def artifact_root() -> Path:
    """解析不公开的文件目录并限制目录访问权限。"""
    configured = Path(get_config().ARTIFACT_STORAGE_DIR).expanduser()
    root = configured if configured.is_absolute() else BASE_DIR / configured
    if root.is_symlink() or root.parent.is_symlink():
        raise ArtifactValidationError("文件存储目录不能是符号链接")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ArtifactValidationError("文件存储目录必须仅供服务进程访问")
    return root


def storage_path(root: Path, key: str) -> Path:
    """仅使用服务端生成的随机键定位文件。"""
    if len(key) != 32 or any(char not in "0123456789abcdef" for char in key):
        raise ArtifactValidationError("无效的文件存储键")
    return root / key


def _validate_office(path: Path, suffix: str) -> None:
    """检查 Office 容器结构和解压上限，不执行其中内容。"""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = {info.filename for info in infos}
            required = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
            if "[Content_Types].xml" not in names or required not in names or len(infos) > 1000:
                raise ArtifactValidationError("Office 文件结构无效")
            if sum(info.file_size for info in infos) > 50 * 1024 * 1024:
                raise ArtifactValidationError("Office 文件解压后超过限制")
            for info in infos:
                parts = Path(info.filename).parts
                if (
                    info.filename.startswith("/") or ".." in parts or "\\" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16) or "vbaproject.bin" in info.filename.lower()
                ):
                    raise ArtifactValidationError("Office 文件包含不允许的内容")
            archive.read("[Content_Types].xml")
            archive.read(required)
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error, NotImplementedError) as exc:
        raise ArtifactValidationError("Office 文件损坏或不可读取") from exc


def validate_file(path: Path, filename: str) -> str:
    """核验扩展名和实际文件结构，返回可信媒体类型。"""
    if not filename or len(filename) > 255 or filename in {".", ".."}:
        raise ArtifactValidationError("文件名无效")
    if Path(filename).name != filename or any(ord(char) < 32 for char in filename) or "\\" in filename:
        raise ArtifactValidationError("文件名包含非法字符")
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise ArtifactValidationError("不支持的文件格式")
    if suffix == ".txt":
        try:
            path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ArtifactValidationError("文本文件必须使用 UTF-8 编码") from exc
    elif suffix == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ArtifactValidationError("PDF 文件签名无效")
    else:
        _validate_office(path, suffix)
    return MEDIA_TYPES[suffix]


async def save_upload(file: UploadFile, owner_id: int, root: Path) -> tuple[Artifact, Path]:
    """限量写入临时文件，校验后原子转入私有目录。"""
    filename = file.filename or ""
    key = uuid4().hex
    temporary = root / f".{key}.tmp"
    final = storage_path(root, key)
    total = 0
    digest = hashlib.sha256()
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ArtifactValidationError("单个文件超过 10 MiB")
                digest.update(chunk)
                stream.write(chunk)
        if total == 0:
            raise ArtifactValidationError("文件不能为空")
        media_type = validate_file(temporary, filename)
        os.replace(temporary, final)
        retention_days = get_config().ARTIFACT_RETENTION_DAYS
        if retention_days < 1:
            raise ArtifactValidationError("文件保留天数必须大于零")
        expires_at = shanghai_now(naive=True) + timedelta(days=retention_days)
        return Artifact(
            id=str(uuid4()), owner_id=owner_id, filename=filename, media_type=media_type, byte_size=total,
            sha256=digest.hexdigest(), storage_key=key, source="upload", state="ready", is_deleted=0, status=1,
            expires_at=expires_at,
        ), final
    except BaseException:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


async def publish_generated_artifact(
    sessions: async_sessionmaker[AsyncSession], run_id: str, publication_key: str, filename: str, content: bytes, root: Path,
) -> Artifact:
    """在已完成 Run 下幂等发布经过格式校验的生成文件。"""
    if not publication_key or len(publication_key) > 128 or not content or len(content) > MAX_FILE_BYTES:
        raise ArtifactValidationError("产物发布参数无效或文件超过 10 MiB")
    digest = hashlib.sha256(content).hexdigest()
    async with sessions() as db:
        existing = await db.scalar(select(Artifact).where(
            Artifact.run_id == run_id, Artifact.publication_key == publication_key,
        ))
        if existing is not None:
            if existing.filename != filename or existing.sha256 != digest or existing.state != "ready":
                raise ArtifactValidationError("产物发布幂等键与已有文件冲突")
            return existing

    key = uuid4().hex
    temporary = root / f".{key}.tmp"
    final = storage_path(root, key)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        media_type = validate_file(temporary, filename)
        os.replace(temporary, final)
        async with sessions.begin() as db:
            run = await db.get(AgentRun, run_id)
            user = await db.get(Admin, run.owner_id) if run is not None else None
            if run is None or run.state != "completed" or user is None or user.status != 1 or user.is_deleted != 0:
                raise ArtifactValidationError("仅可为已完成且所有者有效的运行发布产物")
            retention_days = get_config().ARTIFACT_RETENTION_DAYS
            if retention_days < 1:
                raise ArtifactValidationError("文件保留天数必须大于零")
            expires_at = shanghai_now(naive=True) + timedelta(days=retention_days)
            artifact = Artifact(
                id=str(uuid4()), owner_id=run.owner_id, run_id=run_id, publication_key=publication_key,
                filename=filename, media_type=media_type, byte_size=len(content), sha256=digest, storage_key=key,
                source="generated", state="ready", expires_at=expires_at, is_deleted=0, status=1,
            )
            db.add(artifact)
            await db.flush()
        return artifact
    except BaseException:
        temporary.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise


async def prune_artifacts(sessions: async_sessionmaker[AsyncSession], root: Path, *, limit: int = 1000) -> tuple[int, int]:
    """撤销过期文件并清理宽限期外、未被业务库引用的孤儿文件。"""
    if not 1 <= limit <= 1000:
        raise ValueError("无效的文件清理批量")
    now = shanghai_now(naive=True)
    async with sessions.begin() as db:
        expired = list((await db.scalars(select(Artifact).where(
            Artifact.state == "ready", Artifact.expires_at.is_not(None), Artifact.expires_at <= now,
        ).order_by(Artifact.expires_at, Artifact.id).limit(limit))).all())
        for item in expired:
            item.state = "deleted"
            item.is_deleted = 1
    for item in expired:
        storage_path(root, item.storage_key).unlink(missing_ok=True)

    cutoff = time.time() - ORPHAN_GRACE_SECONDS
    candidates = []
    for path in root.iterdir():
        if len(candidates) >= limit:
            break
        if path.is_symlink() or not path.is_file() or path.stat().st_mtime > cutoff:
            continue
        if path.name.endswith(".tmp") or len(path.name) == 32 and all(char in "0123456789abcdef" for char in path.name):
            candidates.append(path)
    keys = [path.name for path in candidates if not path.name.endswith(".tmp")]
    async with sessions() as db:
        referenced = set((await db.scalars(select(Artifact.storage_key).where(
            Artifact.storage_key.in_(keys), Artifact.state == "ready",
        ))).all()) if keys else set()
    orphaned = 0
    for path in candidates:
        if path.name not in referenced:
            path.unlink(missing_ok=True)
            orphaned += 1
    return len(expired), orphaned
