# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 15:49:22
# @Author  : yangyuexiong
# @File    : artifact_cleanup.py

"""在开发环境盘点和清理当前管理员的本地上传物及产物。"""

import asyncio
import stat
from pathlib import Path

from loguru import logger
from sqlalchemy import func, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import ArtifactCleanupResult, ArtifactOrigin, ArtifactState
from app.models.agent_config import AgentConfig
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.artifact import Artifact
from app.models.knowledge import KnowledgeDocumentVersion
from app.models.run_artifact_read import RunArtifactRead
from app.schemas.artifact_cleanup import ArtifactCleanupFileOut, ArtifactCleanupItemOut, ArtifactCleanupOut, ArtifactCleanupPageOut
from app.services.artifact_store import artifact_root, storage_path
from app.services.identity import audit
from app.services.private_blob_store import using_s3
from app.services.run_manager import TERMINAL_STATES


def require_local_development() -> None:
    """只允许开发环境的本地存储操作，不触及生产配置或远端对象存储。"""
    if not get_config().IS_DEV:
        raise CustomException(status_code=403, custom_code=403, detail="文件盘点与物理清理仅在 development 环境开放")
    if using_s3():
        raise CustomException(status_code=409, custom_code=10005, detail="开发文件清理仅支持本地文件存储")


def _is_regular_file(path: Path) -> bool:
    """不跟随符号链接检查磁盘文件，缺失文件按不可用处理。"""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def _related_run_ids(artifact_ids: list[str]):
    """合并生成、上传引用及成功读取旧产物的运行关系，供在用保护使用。"""
    return union(
        select(Artifact.run_id.label("run_id")).where(Artifact.id.in_(artifact_ids), Artifact.run_id.is_not(None)),
        select(AgentRunMessage.run_id.label("run_id"))
        .join(AgentMessageAttachment, AgentMessageAttachment.message_id == AgentRunMessage.id)
        .where(AgentMessageAttachment.artifact_id.in_(artifact_ids)),
        select(RunArtifactRead.run_id.label("run_id")).where(RunArtifactRead.source_artifact_id.in_(artifact_ids)),
    )


async def list_cleanup_files(
    db: AsyncSession,
    owner_id: int,
    *,
    agent_id: int | None,
    session_id: str | None,
    page: int,
    page_size: int,
) -> ArtifactCleanupPageOut:
    """按 Agent 或会话汇总本人上传引用和生成记录，包含历史已删除、过期及缺失文件。"""
    require_local_development()
    if (agent_id is None) == (session_id is None):
        raise CustomException(status_code=400, custom_code=10006, detail="agent_id 和 session_id 必须且只能提供一个")
    if agent_id is not None:
        target = await db.scalar(select(AgentConfig.id).where(AgentConfig.id == agent_id, AgentConfig.owner_id == owner_id))
    else:
        target = await db.scalar(select(AgentSession.id).where(AgentSession.id == session_id, AgentSession.owner_id == owner_id))
    if target is None:
        raise CustomException(status_code=404, custom_code=10002, detail="智能体或会话不存在")
    run_conditions = [AgentRun.owner_id == owner_id]
    run_conditions.append(AgentRun.agent_id == agent_id if agent_id is not None else AgentRun.session_id == session_id)
    uploaded = (
        select(AgentMessageAttachment.artifact_id)
        .join(AgentRunMessage, AgentRunMessage.id == AgentMessageAttachment.message_id)
        .join(AgentRun, AgentRun.id == AgentRunMessage.run_id)
        .where(*run_conditions, AgentRunMessage.owner_id == owner_id)
    )
    generated = select(Artifact.id).join(AgentRun, AgentRun.id == Artifact.run_id).where(*run_conditions)
    conditions = (
        Artifact.owner_id == owner_id,
        Artifact.source.in_((ArtifactOrigin.UPLOAD, ArtifactOrigin.GENERATED)),
        Artifact.id.in_(union(uploaded, generated)),
    )
    total = await db.scalar(select(func.count()).select_from(Artifact).where(*conditions)) or 0
    files = list(
        await db.scalars(select(Artifact).where(*conditions).order_by(Artifact.create_time, Artifact.id).offset((page - 1) * page_size).limit(page_size)),
    )
    root = artifact_root()
    items = []
    for file in files:
        items.append(
            ArtifactCleanupFileOut(
                artifact_id=file.id,
                filename=file.filename,
                storage_name=file.storage_key,
                sha256=file.sha256,
                source=file.source,
                state=file.state,
                is_deleted=bool(file.is_deleted),
                byte_size=file.byte_size,
                exists_on_disk=_is_regular_file(storage_path(root, file.storage_key)),
                run_id=file.run_id,
                expires_at=file.expires_at,
                create_time=file.create_time,
            ),
        )
    return ArtifactCleanupPageOut(items=items, total=total, page=page, page_size=page_size)


async def _require_idle_files(db: AsyncSession, files: list[Artifact], owner_id: int) -> None:
    """锁定关联会话并阻止未结束运行正在使用或可能续读的文件被清理。"""
    related_runs = _related_run_ids([file.id for file in files])
    session_ids = select(AgentRun.session_id).where(AgentRun.id.in_(related_runs), AgentRun.session_id.is_not(None))
    locked_sessions = list(
        await db.scalars(
            select(AgentSession.id).where(AgentSession.id.in_(session_ids), AgentSession.owner_id == owner_id).order_by(AgentSession.id).with_for_update(),
        ),
    )
    active = await db.scalar(
        select(AgentRun.id)
        .where(
            AgentRun.owner_id == owner_id,
            AgentRun.state.not_in(TERMINAL_STATES),
            or_(AgentRun.id.in_(related_runs), AgentRun.session_id.in_(locked_sessions)),
        )
        .limit(1),
    )
    if active is not None:
        raise CustomException(status_code=409, custom_code=10005, detail="文件关联会话或运行尚未结束，请先取消或等待任务结束")


def _unlink_local_file(path: Path) -> ArtifactCleanupResult:
    """只移除该路径自身，不跟随链接，也不递归删除目录。"""
    try:
        path.unlink()
    except FileNotFoundError:
        return ArtifactCleanupResult.ALREADY_MISSING
    return ArtifactCleanupResult.DELETED


async def cleanup_files(db: AsyncSession, owner_id: int, filenames: list[str]) -> ArtifactCleanupOut:
    """整批预检并撤销访问后逐项物理删除，保留元数据与历史引用供追溯及失败重试。"""
    require_local_development()
    matches = list(
        await db.scalars(
            select(Artifact)
            .where(Artifact.owner_id == owner_id, or_(Artifact.storage_key.in_(filenames), Artifact.filename.in_(filenames)))
            .order_by(Artifact.id)
            .limit(len(filenames) + 1)
            .with_for_update(),
        ),
    )
    if len(matches) > len(filenames):
        raise CustomException(status_code=409, custom_code=10005, detail="原始文件名对应多条记录，请改用 storage_name，整批未清理")
    files = []
    selected_ids = set()
    for name in filenames:
        candidates = [file for file in matches if name in (file.storage_key, file.filename)]
        if not candidates:
            raise CustomException(status_code=404, custom_code=10002, detail="文件不存在或不属于当前用户，整批未清理")
        if len(candidates) != 1:
            raise CustomException(status_code=409, custom_code=10005, detail="原始文件名对应多条记录，请改用 storage_name，整批未清理")
        file = candidates[0]
        if file.source not in (ArtifactOrigin.UPLOAD, ArtifactOrigin.GENERATED):
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 版本资源不能通过文件清理接口删除")
        if file.id not in selected_ids:
            files.append(file)
            selected_ids.add(file.id)
    knowledge = await db.scalar(select(KnowledgeDocumentVersion.id).where(KnowledgeDocumentVersion.artifact_id.in_(selected_ids)).limit(1))
    if knowledge is not None:
        raise CustomException(status_code=409, custom_code=10005, detail="知识库原始文件须通过知识文档删除接口管理")
    await _require_idle_files(db, files, owner_id)
    root = artifact_root()
    for file in files:
        path = storage_path(root, file.storage_key)
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(mode):
            raise CustomException(status_code=409, custom_code=10005, detail="只允许清理普通文件，不能删除目录或符号链接")
    targets = [(file.id, file.filename, file.storage_key) for file in files]
    for file in files:
        file.state = ArtifactState.DELETED
        file.is_deleted = 1
        audit(db, "artifact.dev_cleanup_requested", actor_id=owner_id, subject=file.id)
    await db.commit()
    items = []
    for artifact_id, filename, storage_name in targets:
        try:
            result = await asyncio.to_thread(_unlink_local_file, storage_path(root, storage_name))
        except OSError as exc:
            result = ArtifactCleanupResult.FAILED
            logger.warning("开发文件清理失败：{} {}", artifact_id, type(exc).__name__)
        audit(db, f"artifact.dev_cleanup_{result.value}", actor_id=owner_id, subject=artifact_id)
        items.append(ArtifactCleanupItemOut(artifact_id=artifact_id, filename=filename, storage_name=storage_name, result=result))
    await db.commit()
    return ArtifactCleanupOut(
        items=items,
        deleted_count=sum(item.result == ArtifactCleanupResult.DELETED for item in items),
        already_missing_count=sum(item.result == ArtifactCleanupResult.ALREADY_MISSING for item in items),
        failed_count=sum(item.result == ArtifactCleanupResult.FAILED for item in items),
    )
