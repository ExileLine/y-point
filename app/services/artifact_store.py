# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : artifact_store.py

"""管理受限上传及私有文件路径。"""

import hashlib
import asyncio
import os
import shutil
import stat
import time
import zipfile
import zlib
from PIL import Image, UnidentifiedImageError
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import UploadFile
from loguru import logger
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import BASE_DIR, get_config
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.artifact import Artifact
from app.models.artifact_source import ArtifactSource
from app.models.base import shanghai_now
from app.models.run_artifact_read import RunArtifactRead
from app.services.private_blob_store import delete_blob, old_blob_batches, store_blob, using_s3

MAX_FILES = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
ORPHAN_GRACE_SECONDS = 3600
MEDIA_TYPES = {
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
GENERATED_TEXT_MEDIA_TYPES = {
    ".md": "text/markdown", ".html": "text/html", ".csv": "text/csv", ".json": "application/json",
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
            if "[Content_Types].xml" not in names or required not in names or len(infos) > 1000 or len(names) != len(infos):
                raise ArtifactValidationError("Office 文件结构无效")
            if sum(info.file_size for info in infos) > 50 * 1024 * 1024:
                raise ArtifactValidationError("Office 文件解压后超过限制")
            expanded = 0
            for info in infos:
                parts = Path(info.filename).parts
                if (
                    info.filename.startswith("/") or ".." in parts or "\\" in info.filename
                    or stat.S_ISLNK(info.external_attr >> 16) or "vbaproject.bin" in info.filename.lower()
                    or info.flag_bits & 1
                ):
                    raise ArtifactValidationError("Office 文件包含不允许的内容")
                with archive.open(info) as stream:
                    while chunk := stream.read(1024 * 1024):
                        expanded += len(chunk)
                        if expanded > 50 * 1024 * 1024:
                            raise ArtifactValidationError("Office 文件解压后超过限制")
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error, NotImplementedError) as exc:
        raise ArtifactValidationError("Office 文件损坏或不可读取") from exc


def validate_file(path: Path, filename: str, *, generated: bool = False) -> str:
    """核验扩展名和实际文件结构，返回可信媒体类型。"""
    if not filename or len(filename) > 255 or filename in {".", ".."}:
        raise ArtifactValidationError("文件名无效")
    if Path(filename).name != filename or any(ord(char) < 32 for char in filename) or "\\" in filename:
        raise ArtifactValidationError("文件名包含非法字符")
    suffix = Path(filename).suffix.lower()
    allowed_types = MEDIA_TYPES | GENERATED_TEXT_MEDIA_TYPES if generated else MEDIA_TYPES
    if suffix not in allowed_types:
        raise ArtifactValidationError("不支持的文件格式")
    if suffix in {".txt", ".md", ".html", ".csv", ".json"}:
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ArtifactValidationError("文本文件必须使用 UTF-8 编码") from exc
        if "\x00" in content:
            raise ArtifactValidationError("文本文件包含二进制内容")
    elif suffix == ".pdf":
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ArtifactValidationError("PDF 文件签名无效")
            stream.seek(max(0, path.stat().st_size - 1024))
            if b"%%EOF" not in stream.read():
                raise ArtifactValidationError("PDF 文件缺少结束标记")
    elif suffix in {".png", ".jpg", ".jpeg"}:
        if path.stat().st_size > MAX_IMAGE_BYTES:
            raise ArtifactValidationError("图片超过 2 MiB")
        try:
            if suffix == ".png":
                with path.open("rb") as stream:
                    stream.seek(-12, os.SEEK_END)
                    if stream.read() != b"\x00\x00\x00\x00IEND\xaeB`\x82":
                        raise ArtifactValidationError("PNG 文件缺少结束块")
            with Image.open(path) as image:
                if image.format != ("PNG" if suffix == ".png" else "JPEG") or getattr(image, "n_frames", 1) != 1:
                    raise ArtifactValidationError("图片格式与扩展名不一致")
                if not 1 <= image.width <= 2048 or not 1 <= image.height <= 2048:
                    raise ArtifactValidationError("图片尺寸超过限制")
                image.load()
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ArtifactValidationError("图片损坏或不可读取") from exc
    else:
        _validate_office(path, suffix)
    return allowed_types[suffix]


async def scan_file(path: Path) -> None:
    """按配置调用 ClamAV；启用时无扫描器、超时或错误均拒绝文件。"""
    if not get_config().ARTIFACT_REQUIRE_MALWARE_SCAN:
        return
    scanner = shutil.which("clamscan")
    if scanner is None:
        raise ArtifactValidationError("文件扫描器不可用")
    try:
        process = await asyncio.create_subprocess_exec(
            scanner, "--no-summary", str(path), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise ArtifactValidationError("文件扫描器不可用") from None
    try:
        code = await asyncio.wait_for(process.wait(), timeout=30)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ArtifactValidationError("文件扫描超时") from None
    if code != 0:
        raise ArtifactValidationError("文件未通过恶意内容扫描" if code == 1 else "文件扫描失败")


async def enforce_owner_quota(db: AsyncSession, owner_id: int, *, add_count: int, add_bytes: int) -> None:
    """锁定所有者记录后核算仍有效文件的数量与字节数。"""
    config = get_config()
    if config.ARTIFACT_OWNER_MAX_FILES < 1 or config.ARTIFACT_OWNER_MAX_BYTES < 1:
        raise ArtifactValidationError("文件配额配置无效")
    owner = await db.scalar(select(Admin.id).where(Admin.id == owner_id).with_for_update())
    if owner is None:
        raise ArtifactValidationError("文件所有者不存在")
    now = shanghai_now(naive=True)
    count, total = (await db.execute(select(func.count(Artifact.id), func.coalesce(func.sum(Artifact.byte_size), 0)).where(
        Artifact.owner_id == owner_id, Artifact.state.in_(("ready", "pending")), Artifact.is_deleted == 0,
        or_(Artifact.expires_at.is_(None), Artifact.expires_at > now),
    ))).one()
    if count + add_count > config.ARTIFACT_OWNER_MAX_FILES or total + add_bytes > config.ARTIFACT_OWNER_MAX_BYTES:
        raise ArtifactValidationError("文件数量或存储容量超过用户配额")


async def _cleanup_uncommitted_blob(root: Path, key: str, *, remote_attempted: bool) -> None:
    """清理失败写入；清理故障交由孤儿扫描重试，不覆盖原始异常。"""
    for path in (root / f".{key}.tmp", storage_path(root, key)):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("未提交文件的本地清理失败：{}", type(exc).__name__)
    if remote_attempted:
        try:
            await delete_blob(root, key)
        except Exception as exc:
            logger.warning("未提交文件的对象清理失败：{}", type(exc).__name__)


async def save_upload(file: UploadFile, owner_id: int, root: Path) -> tuple[Artifact, Path]:
    """限量写入临时文件，校验后原子转入私有目录。"""
    filename = file.filename or ""
    key = uuid4().hex
    temporary = root / f".{key}.tmp"
    final = storage_path(root, key)
    total = 0
    digest = hashlib.sha256()
    remote_attempted = False
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
        await scan_file(temporary)
        if using_s3():
            remote_attempted = True
            await store_blob(root, key, temporary)
            temporary.unlink(missing_ok=True)
        else:
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
        await _cleanup_uncommitted_blob(root, key, remote_attempted=remote_attempted)
        raise
    finally:
        await file.close()


async def publish_generated_artifact(
    sessions: async_sessionmaker[AsyncSession], run_id: str, publication_key: str, filename: str, content: bytes, root: Path,
    *, source_artifact_id: str | None = None,
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
            if (
                existing.filename != filename or existing.sha256 != digest or existing.state != "ready"
                or existing.source_artifact_id != source_artifact_id
            ):
                raise ArtifactValidationError("产物发布幂等键与已有文件冲突")
            return existing

    key = uuid4().hex
    temporary = root / f".{key}.tmp"
    final = storage_path(root, key)
    remote_attempted = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        media_type = validate_file(temporary, filename)
        await scan_file(temporary)
        if using_s3():
            remote_attempted = True
            await store_blob(root, key, temporary)
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, final)
        async with sessions.begin() as db:
            run = await db.get(AgentRun, run_id)
            user = await db.get(Admin, run.owner_id) if run is not None else None
            if run is None or run.state != "completed" or user is None or user.status != 1 or user.is_deleted != 0:
                raise ArtifactValidationError("仅可为已完成且所有者有效的运行发布产物")
            if source_artifact_id is not None:
                source = await db.get(Artifact, source_artifact_id)
                if (
                    source is None or source.owner_id != run.owner_id or source.source != "upload"
                    or source.state != "ready" or source.is_deleted != 0
                    or source.expires_at is not None and source.expires_at <= shanghai_now(naive=True)
                ):
                    raise ArtifactValidationError("来源文件不可用")
            retention_days = get_config().ARTIFACT_RETENTION_DAYS
            if retention_days < 1:
                raise ArtifactValidationError("文件保留天数必须大于零")
            expires_at = shanghai_now(naive=True) + timedelta(days=retention_days)
            await enforce_owner_quota(db, run.owner_id, add_count=1, add_bytes=len(content))
            artifact = Artifact(
                id=str(uuid4()), owner_id=run.owner_id, run_id=run_id, publication_key=publication_key,
                source_artifact_id=source_artifact_id,
                filename=filename, media_type=media_type, byte_size=len(content), sha256=digest, storage_key=key,
                source="generated", state="ready", expires_at=expires_at, is_deleted=0, status=1,
            )
            db.add(artifact)
            await db.flush()
            result = await db.execute(update(AgentRun).where(
                AgentRun.id == run_id, AgentRun.state == "completed", AgentRun.version == run.version,
            ).values(version=run.version + 1, event_seq=run.event_seq + 1).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise ArtifactValidationError("运行状态已变化")
            db.add(AgentRunEvent(
                run_id=run_id, sequence=run.event_seq + 1, protocol_version=1,
                event_type="artifact_published", state="completed", artifact_id=artifact.id,
            ))
            await db.flush()
        return artifact
    except IntegrityError:
        await _cleanup_uncommitted_blob(root, key, remote_attempted=remote_attempted)
        async with sessions() as db:
            existing = await db.scalar(select(Artifact).where(
                Artifact.run_id == run_id, Artifact.publication_key == publication_key,
            ))
        if (
            existing is not None and existing.filename == filename and existing.sha256 == digest
            and existing.state == "ready" and existing.source_artifact_id == source_artifact_id
        ):
            return existing
        raise ArtifactValidationError("产物发布幂等键与已有文件冲突") from None
    except BaseException:
        await _cleanup_uncommitted_blob(root, key, remote_attempted=remote_attempted)
        raise


async def stage_generated_text_artifact(
    sessions: async_sessionmaker[AsyncSession], run_id: str, owner_id: int, lease_token: str,
    filename: str, content: str, root: Path,
) -> Artifact:
    """保留 TXT 工具的暂存入口与发布键。"""
    if Path(filename).suffix.lower() != ".txt":
        raise ArtifactValidationError("生成文件必须是 TXT")
    return await stage_generated_artifact(sessions, run_id, owner_id, lease_token, filename, content.encode("utf-8"), root)


async def stage_generated_artifact(
    sessions: async_sessionmaker[AsyncSession], run_id: str, owner_id: int, lease_token: str,
    filename: str, data: bytes, root: Path,
) -> Artifact:
    """幂等暂存已生成文件；Run 完成前不公开下载。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in MEDIA_TYPES | GENERATED_TEXT_MEDIA_TYPES or len(filename) > 120:
        raise ArtifactValidationError("生成文件格式不支持或文件名超过 120 字符")
    if not data or len(data) > MAX_FILE_BYTES:
        raise ArtifactValidationError("生成文件为空或超过 10 MiB")
    prefix = "model-txt-" if suffix == ".txt" else "model-file-"
    publication_key = prefix + hashlib.sha256(filename.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(data).hexdigest()

    async def existing_artifact() -> Artifact | None:
        async with sessions() as db:
            return await db.scalar(select(Artifact).where(
                Artifact.run_id == run_id, Artifact.publication_key == publication_key,
            ))

    existing = await existing_artifact()
    if existing is not None:
        if existing.sha256 != digest or existing.filename != filename or existing.state != "pending":
            raise ArtifactValidationError("同名生成文件内容冲突")
        return existing

    key = uuid4().hex
    temporary = root / f".{key}.tmp"
    remote_attempted = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        media_type = validate_file(temporary, filename, generated=True)
        await scan_file(temporary)
        if using_s3():
            remote_attempted = True
            await store_blob(root, key, temporary)
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, storage_path(root, key))
        async with sessions.begin() as db:
            run = await db.get(AgentRun, run_id)
            now = shanghai_now(naive=True)
            if (
                run is None or run.owner_id != owner_id or run.state != "running"
                or run.lease_token != lease_token or run.lease_expires_at is None or run.lease_expires_at <= now
            ):
                raise ArtifactValidationError("运行授权已失效")
            count = await db.scalar(select(func.count(Artifact.id)).where(
                Artifact.run_id == run_id, Artifact.source == "generated", Artifact.state == "pending",
            ))
            if count >= MAX_FILES:
                raise ArtifactValidationError("单轮生成文件超过数量限制")
            await enforce_owner_quota(db, owner_id, add_count=1, add_bytes=len(data))
            reads = (await db.scalars(select(RunArtifactRead.source_artifact_id).where(
                RunArtifactRead.run_id == run_id, RunArtifactRead.owner_id == owner_id,
            ).order_by(RunArtifactRead.id))).all()
            source_ids = list(dict.fromkeys(reads))
            artifact = Artifact(
                id=str(uuid4()), owner_id=owner_id, run_id=run_id, publication_key=publication_key,
                filename=filename, media_type=media_type, byte_size=len(data), sha256=digest, storage_key=key,
                source="generated", state="pending", expires_at=now + timedelta(hours=1), is_deleted=0, status=1,
            )
            db.add(artifact)
            await db.flush()
            db.add_all([
                ArtifactSource(
                    target_artifact_id=artifact.id, source_artifact_id=source_id,
                    run_id=run_id, owner_id=owner_id, position=position,
                )
                for position, source_id in enumerate(source_ids)
            ])
        return artifact
    except IntegrityError:
        await _cleanup_uncommitted_blob(root, key, remote_attempted=remote_attempted)
        existing = await existing_artifact()
        if existing is not None and existing.sha256 == digest and existing.filename == filename and existing.state == "pending":
            return existing
        raise ArtifactValidationError("同名生成文件内容冲突") from None
    except BaseException:
        await _cleanup_uncommitted_blob(root, key, remote_attempted=remote_attempted)
        raise


async def publish_pending_answers(
    sessions: async_sessionmaker[AsyncSession], root: Path, *, limit: int = 100,
) -> int:
    """补发已完成 Run 的最终文本产物；重复扫描使用固定发布键。"""
    if not 1 <= limit <= 1000:
        raise ValueError("无效的产物补发数量")
    published = 0
    now = shanghai_now(naive=True)
    async with sessions() as db:
        existing = select(Artifact.id).where(
            Artifact.run_id == AgentRun.id, Artifact.publication_key == "final-answer-v1",
        ).exists()
        statement = select(AgentRun.id).where(
            AgentRun.state == "completed", AgentRun.publish_answer_artifact.is_(True), ~existing,
            or_(AgentRun.answer_artifact_retry_at.is_(None), AgentRun.answer_artifact_retry_at <= now),
        ).order_by(AgentRun.update_time, AgentRun.id).limit(limit)
        run_ids = (await db.scalars(statement)).all()
    for run_id in run_ids:
        async with sessions() as db:
            answer = await db.scalar(select(AgentRunMessage.content).where(
                AgentRunMessage.run_id == run_id, AgentRunMessage.role == "assistant",
            ))
        if answer is None:
            async with sessions.begin() as db:
                await db.execute(update(AgentRun).where(AgentRun.id == run_id).values(
                    answer_artifact_retry_at=shanghai_now(naive=True) + timedelta(hours=1),
                ))
            continue
        try:
            await publish_generated_artifact(
                sessions, run_id, "final-answer-v1", "answer.txt", answer.encode("utf-8"), root,
            )
        except (ArtifactValidationError, OSError, Boto3Error, BotoCoreError, ClientError) as exc:
            logger.warning("运行 {} 的回答产物未发布：{}", run_id, type(exc).__name__)
            async with sessions.begin() as db:
                await db.execute(update(AgentRun).where(AgentRun.id == run_id).values(
                    answer_artifact_retry_at=shanghai_now(naive=True) + timedelta(hours=1),
                ))
            continue
        published += 1
    return published


async def _remove_unreferenced_local_files(
    sessions: async_sessionmaker[AsyncSession], paths: list[Path], *, limit: int,
) -> int:
    """按文件存储键批量核对引用，最多删除本批中的 limit 个孤儿。"""
    keys = [path.name for path in paths if not path.name.endswith(".tmp")]
    async with sessions() as db:
        referenced = set((await db.scalars(select(Artifact.storage_key).where(
            Artifact.storage_key.in_(keys), Artifact.state == "ready",
        ))).all()) if keys else set()
    removed = 0
    for path in paths:
        if path.name not in referenced:
            path.unlink(missing_ok=True)
            removed += 1
            if removed >= limit:
                break
    return removed


async def prune_artifacts(sessions: async_sessionmaker[AsyncSession], root: Path, *, limit: int = 1000) -> tuple[int, int]:
    """撤销过期文件并清理宽限期外、未被业务库引用的孤儿文件。"""
    if not 1 <= limit <= 1000:
        raise ValueError("无效的文件清理批量")
    now = shanghai_now(naive=True)
    async with sessions.begin() as db:
        expired = list((await db.scalars(select(Artifact).where(
            Artifact.state.in_(("ready", "pending")), Artifact.expires_at.is_not(None), Artifact.expires_at <= now,
        ).order_by(Artifact.expires_at, Artifact.id).limit(limit))).all())
        for item in expired:
            item.state = "deleted"
            item.is_deleted = 1
    for item in expired:
        await delete_blob(root, item.storage_key)

    cutoff = time.time() - ORPHAN_GRACE_SECONDS
    candidates = []
    orphaned = 0
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.stat().st_mtime > cutoff:
            continue
        name = path.name
        is_key = len(name) == 32 and all(char in "0123456789abcdef" for char in name)
        is_temporary = (
            len(name) == 37 and name.startswith(".") and name.endswith(".tmp")
            and all(char in "0123456789abcdef" for char in name[1:-4])
        )
        if not (is_key or is_temporary):
            continue
        candidates.append(path)
        if len(candidates) >= limit:
            orphaned += await _remove_unreferenced_local_files(sessions, candidates, limit=limit - orphaned)
            candidates.clear()
            if orphaned >= limit:
                return len(expired), orphaned
    if candidates:
        orphaned += await _remove_unreferenced_local_files(sessions, candidates, limit=limit - orphaned)
        if orphaned >= limit:
            return len(expired), orphaned
    if using_s3():
        async for old_keys in old_blob_batches(
            before=shanghai_now() - timedelta(seconds=ORPHAN_GRACE_SECONDS), batch_size=limit,
        ):
            async with sessions() as db:
                referenced = set((await db.scalars(select(Artifact.storage_key).where(
                    Artifact.storage_key.in_(old_keys), Artifact.state == "ready",
                ))).all()) if old_keys else set()
            for key in old_keys:
                if key not in referenced:
                    await delete_blob(root, key)
                    orphaned += 1
                    if orphaned >= limit:
                        return len(expired), orphaned
    return len(expired), orphaned
