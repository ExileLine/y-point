# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : artifacts.py

"""提供私有附件上传、查询与授权下载。"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, Response
from botocore.exceptions import BotoCoreError, ClientError
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import quote

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.schemas.artifact import ArtifactOut
from app.services.artifact_store import (
    MAX_FILES,
    MAX_FILE_BYTES,
    ArtifactValidationError,
    artifact_root,
    enforce_owner_quota,
    save_upload,
    storage_path,
)
from app.services.artifact_sources import load_read_source_artifact_ids
from app.services.message_attachments import load_run_context_artifact_ids
from app.services.private_blob_store import delete_blob, read_blob, using_s3

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


async def _owned_artifact(db: AsyncSession, artifact_id: str, owner_id: int) -> Artifact:
    """将不存在、过期和跨用户文件统一隐藏为 404。"""
    artifact = await db.get(Artifact, artifact_id)
    if (
        artifact is None
        or artifact.owner_id != owner_id
        or artifact.state != "ready"
        or artifact.is_deleted != 0
        or artifact.expires_at is not None
        and artifact.expires_at <= shanghai_now(naive=True)
    ):
        raise CustomException(status_code=404, custom_code=10002, detail="文件不存在")
    return artifact


async def _cleanup_failed_uploads(saved: list[tuple[Artifact, Path]], root: Path) -> None:
    """逐项尝试清理整批文件，保留上传或提交时的原始错误。"""
    for item, path in saved:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("上传回滚后的本地清理失败：{}", type(exc).__name__)
        try:
            await delete_blob(root, item.storage_key)
        except Exception as exc:
            logger.warning("上传回滚后的对象清理失败：{}", type(exc).__name__)


@router.post("/uploads", summary="上传多个私有文件")
async def upload_artifacts(
    request: Request,
    files: list[UploadFile] = File(description="本批上传文件，最多 5 个，每个最多 10 MiB"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """校验并持久化本批文件；任一失败时清理本批写入。"""
    length = request.headers.get("content-length")
    if length is not None and length.isdecimal() and int(length) > MAX_FILES * MAX_FILE_BYTES + 1024 * 1024:
        raise CustomException(status_code=413, custom_code=10005, detail="上传请求超过大小限制")
    if not 1 <= len(files) <= MAX_FILES:
        raise CustomException(status_code=400, custom_code=10006, detail="每批只允许上传 1 到 5 个文件")
    root = artifact_root()
    saved: list[tuple[Artifact, Path]] = []
    try:
        for file in files:
            saved.append(await save_upload(file, actor.id, root))
        await enforce_owner_quota(db, actor.id, add_count=len(saved), add_bytes=sum((item.byte_size for item, _ in saved)))
        db.add_all([item for item, _ in saved])
        await db.commit()
    except ArtifactValidationError as exc:
        await db.rollback()
        await _cleanup_failed_uploads(saved, root)
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    except BaseException:
        await db.rollback()
        await _cleanup_failed_uploads(saved, root)
        raise
    return api_response(
        http_code=201,
        code=201,
        data=[ArtifactOut.model_validate(item).model_dump(mode="json") for item, _ in saved],
        is_pop=False,
    )


@router.get("/{artifact_id}", summary="查询私有文件元数据")
async def read_artifact(artifact_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """仅文件所有者可以查看不含存储路径的元数据。"""
    item = await _owned_artifact(db, artifact_id, actor.id)
    data = ArtifactOut.model_validate(item).model_dump(mode="json")
    if item.source == "generated" and item.run_id is not None:
        data["context_artifact_ids"] = (await load_run_context_artifact_ids(db, actor.id, [item.run_id])).get(item.run_id, [])
        data["read_source_artifact_ids"] = (await load_read_source_artifact_ids(db, actor.id, [item.id])).get(item.id, [])
    return api_response(data=data, datetime_format=False)


@router.get("", summary="查询运行产物列表")
async def list_run_artifacts(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """只向运行所有者列出仍有效的已发布产物引用。"""
    run = await db.get(AgentRun, run_id)
    if run is None or run.owner_id != actor.id:
        raise CustomException(status_code=404, custom_code=10002, detail="运行不存在")
    statement = (
        select(Artifact)
        .where(
            Artifact.run_id == run_id,
            Artifact.owner_id == actor.id,
            Artifact.source == "generated",
            Artifact.state == "ready",
            Artifact.is_deleted == 0,
            (Artifact.expires_at.is_(None) | (Artifact.expires_at > shanghai_now(naive=True))),
        )
        .order_by(Artifact.create_time, Artifact.id)
    )
    items = (await db.scalars(statement)).all()
    context_ids = (await load_run_context_artifact_ids(db, actor.id, [run_id])).get(run_id, [])
    read_sources = await load_read_source_artifact_ids(db, actor.id, [item.id for item in items])
    return api_response(
        data=[
            {
                **ArtifactOut.model_validate(item).model_dump(mode="json"),
                "context_artifact_ids": context_ids,
                "read_source_artifact_ids": read_sources.get(item.id, []),
            }
            for item in items
        ],
        is_pop=False,
    )


@router.get("/{artifact_id}/download", summary="下载私有文件")
async def download_artifact(artifact_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """逐次校验所有权后返回文件字节流，下载响应保留文件媒体类型。"""
    item = await _owned_artifact(db, artifact_id, actor.id)
    if using_s3():
        try:
            content = await read_blob(artifact_root(), item.storage_key, max_bytes=MAX_FILE_BYTES)
        except (OSError, ValueError, BotoCoreError, ClientError):
            raise CustomException(status_code=404, custom_code=10002, detail="文件不存在") from None
        return Response(
            content,
            media_type=item.media_type,
            headers={
                "Content-Disposition": f"attachment; filename*=utf-8''{quote(item.filename)}",
                "X-Content-Type-Options": "nosniff",
            },
        )
    path = storage_path(artifact_root(), item.storage_key)
    if not path.is_file() or path.is_symlink():
        raise CustomException(status_code=404, custom_code=10002, detail="文件不存在")
    return FileResponse(
        path,
        media_type=item.media_type,
        filename=item.filename,
        headers={
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{artifact_id}", summary="删除私有文件")
async def delete_artifact(artifact_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """先撤销访问，再尽力移除私有文件正文。"""
    item = await _owned_artifact(db, artifact_id, actor.id)
    item.state = "deleted"
    item.is_deleted = 1
    await db.commit()
    try:
        await delete_blob(artifact_root(), item.storage_key)
    except (OSError, BotoCoreError, ClientError) as exc:
        logger.warning("已撤销文件 {} 的访问，但文件清理失败：{}", artifact_id, type(exc).__name__)
    return api_response(
        data={
            "id": artifact_id,
        }
    )
