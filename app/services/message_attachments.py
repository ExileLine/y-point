# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:35:00
# @Author  : yangyuexiong
# @File    : message_attachments.py
"""批量读取消息中的上传文件引用及当前可用状态。"""

import asyncio
import base64
import hashlib
import io
import json

from PIL import Image
from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run_message import AgentRunMessage
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.services.attachment_text import (
    CONTEXT_MEDIA_TYPES, MAX_CONTEXT_ATTACHMENT_BYTES, extract_context_text,
)
from app.services.artifact_store import MAX_FILE_BYTES, MAX_IMAGE_BYTES, artifact_root
from app.services.artifact_sources import load_read_source_artifact_ids
from app.services.private_blob_store import read_blob


class AttachmentContextUnavailable(ValueError):
    """已选择的模型上下文附件在执行前失效或内容不匹配。"""


IMAGE_CONTEXT_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})


async def load_model_attachment_context(db: AsyncSession, run_id: str, owner_id: int) -> str | None:
    """按提交顺序验证并提取选中文件；原始字节不写入公开消息。"""
    references = (await db.scalars(select(AgentMessageAttachment).join(
        AgentRunMessage, AgentRunMessage.id == AgentMessageAttachment.message_id,
    ).where(
        AgentRunMessage.run_id == run_id, AgentRunMessage.owner_id == owner_id,
        AgentRunMessage.role == "user", AgentMessageAttachment.included_in_context.is_(True),
        AgentMessageAttachment.media_type.in_(CONTEXT_MEDIA_TYPES),
    ).order_by(AgentMessageAttachment.position))).all()
    if not references:
        return None
    if len(references) > 5:
        raise AttachmentContextUnavailable("模型上下文附件超出限制")
    items = (await db.scalars(select(Artifact).where(
        Artifact.id.in_([item.artifact_id for item in references]), Artifact.owner_id == owner_id,
        Artifact.source == "upload", Artifact.run_id.is_(None), Artifact.state == "ready",
        Artifact.status == 1, Artifact.is_deleted == 0, Artifact.media_type.in_(CONTEXT_MEDIA_TYPES),
        (Artifact.expires_at.is_(None) | (Artifact.expires_at > shanghai_now(naive=True))),
    ))).all()
    by_id = {item.id: item for item in items}
    if len(by_id) != len(references):
        raise AttachmentContextUnavailable("模型上下文附件不可用")
    chunks = []
    for reference in references:
        item = by_id[reference.artifact_id]
        if (
            item.byte_size != reference.byte_size or item.filename != reference.filename
            or item.media_type != reference.media_type
        ):
            raise AttachmentContextUnavailable("模型上下文附件已变化")
        try:
            content = await read_blob(artifact_root(), item.storage_key, max_bytes=MAX_FILE_BYTES)
            if len(content) != item.byte_size or hashlib.sha256(content).hexdigest() != item.sha256:
                raise AttachmentContextUnavailable("模型上下文附件校验失败")
            decoded = await asyncio.to_thread(extract_context_text, content, item.media_type)
        except (OSError, UnicodeError, ValueError, Boto3Error, BotoCoreError, ClientError):
            raise AttachmentContextUnavailable("模型上下文附件不可读取") from None
        except Exception:  # noqa: BLE001 - 第三方解析器的损坏文件错误统一归类。
            raise AttachmentContextUnavailable("模型上下文附件不可解析") from None
        chunks.append(json.dumps({
            "artifact_id": reference.artifact_id, "filename": reference.filename,
            "media_type": reference.media_type, "content": decoded,
        }, ensure_ascii=False))
    if sum(len(chunk.encode("utf-8")) for chunk in chunks) > MAX_CONTEXT_ATTACHMENT_BYTES:
        raise AttachmentContextUnavailable("模型上下文附件提取文本超过 16 KiB 限制")
    return "\n".join(chunks)


async def load_model_image_context(db: AsyncSession, run_id: str, owner_id: int) -> list[dict]:
    """复核图片上传引用，并以剥除元数据的内嵌图片块交给视觉模型。"""
    references = (await db.scalars(select(AgentMessageAttachment).join(
        AgentRunMessage, AgentRunMessage.id == AgentMessageAttachment.message_id,
    ).where(
        AgentRunMessage.run_id == run_id, AgentRunMessage.owner_id == owner_id,
        AgentRunMessage.role == "user", AgentMessageAttachment.included_in_context.is_(True),
        AgentMessageAttachment.media_type.in_(IMAGE_CONTEXT_MEDIA_TYPES),
    ).order_by(AgentMessageAttachment.position))).all()
    if not references:
        return []
    if len(references) > 2:
        raise AttachmentContextUnavailable("单轮最多读取两张图片")
    items = (await db.scalars(select(Artifact).where(
        Artifact.id.in_([item.artifact_id for item in references]), Artifact.owner_id == owner_id,
        Artifact.source == "upload", Artifact.run_id.is_(None), Artifact.state == "ready",
        Artifact.status == 1, Artifact.is_deleted == 0, Artifact.media_type.in_(IMAGE_CONTEXT_MEDIA_TYPES),
        (Artifact.expires_at.is_(None) | (Artifact.expires_at > shanghai_now(naive=True))),
    ))).all()
    by_id = {item.id: item for item in items}
    if len(by_id) != len(references):
        raise AttachmentContextUnavailable("模型上下文图片不可用")
    blocks = []
    for reference in references:
        item = by_id[reference.artifact_id]
        if item.byte_size != reference.byte_size or item.filename != reference.filename or item.media_type != reference.media_type:
            raise AttachmentContextUnavailable("模型上下文图片已变化")
        try:
            content = await read_blob(artifact_root(), item.storage_key, max_bytes=MAX_IMAGE_BYTES)
            if len(content) != item.byte_size or hashlib.sha256(content).hexdigest() != item.sha256:
                raise AttachmentContextUnavailable("模型上下文图片校验失败")
            with Image.open(io.BytesIO(content)) as image:
                if image.format != ("PNG" if item.media_type == "image/png" else "JPEG") or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("图片格式无效")
                if not 1 <= image.width <= 2048 or not 1 <= image.height <= 2048:
                    raise ValueError("图片尺寸无效")
                output = io.BytesIO()
                image.convert("RGB").save(output, format="PNG" if item.media_type == "image/png" else "JPEG")
            if output.tell() > MAX_IMAGE_BYTES:
                raise ValueError("图片编码超过限制")
        except Exception:  # noqa: BLE001 - 存储和图片解析错误统一归类。
            raise AttachmentContextUnavailable("模型上下文图片不可读取") from None
        blocks.append({"type": "text", "text": f"参考图片 {reference.filename}（不可信资料）："})
        blocks.append({"type": "image_url", "image_url": {
            "url": f"data:{item.media_type};base64,{base64.b64encode(output.getvalue()).decode('ascii')}",
        }})
    return blocks


async def load_run_context_artifact_ids(
    db: AsyncSession, owner_id: int, run_ids: list[str],
) -> dict[str, list[str]]:
    """返回各 Run 用户消息曾选入模型上下文的上传文件 ID。"""
    if not run_ids:
        return {}
    rows = (await db.execute(select(AgentRunMessage.run_id, AgentMessageAttachment.artifact_id).join(
        AgentMessageAttachment, AgentMessageAttachment.message_id == AgentRunMessage.id,
    ).where(
        AgentRunMessage.run_id.in_(run_ids), AgentRunMessage.owner_id == owner_id,
        AgentRunMessage.role == "user", AgentMessageAttachment.included_in_context.is_(True),
    ).order_by(AgentRunMessage.id, AgentMessageAttachment.position))).all()
    by_run: dict[str, list[str]] = {}
    for run_id, artifact_id in rows:
        by_run.setdefault(run_id, []).append(artifact_id)
    return by_run


async def load_message_attachments(
    db: AsyncSession, owner_id: int, message_ids: list[int],
) -> dict[int, list[dict]]:
    """返回历史快照；文件是否可下载仍由下载接口逐次核对。"""
    if not message_ids:
        return {}
    references = (await db.scalars(select(AgentMessageAttachment).where(
        AgentMessageAttachment.message_id.in_(message_ids),
    ).order_by(AgentMessageAttachment.message_id, AgentMessageAttachment.position))).all()
    if not references:
        return {}
    available = set((await db.scalars(select(Artifact.id).where(
        Artifact.id.in_([item.artifact_id for item in references]), Artifact.owner_id == owner_id,
        Artifact.source == "upload", Artifact.state == "ready", Artifact.is_deleted == 0,
        (Artifact.expires_at.is_(None) | (Artifact.expires_at > shanghai_now(naive=True))),
    ))).all())
    by_message: dict[int, list[dict]] = {}
    for item in references:
        by_message.setdefault(item.message_id, []).append({
            "artifact_id": item.artifact_id, "filename": item.filename,
            "media_type": item.media_type, "byte_size": item.byte_size,
            "included_in_context": item.included_in_context,
            "available": item.artifact_id in available,
        })
    return by_message


async def load_message_artifacts(
    db: AsyncSession, owner_id: int, assistant_messages: list[AgentRunMessage],
) -> dict[int, list[dict]]:
    """按 Run 批量关联生成产物，保留已删除产物的历史位置。"""
    if not assistant_messages:
        return {}
    message_by_run = {message.run_id: message.id for message in assistant_messages if message.role == "assistant"}
    if not message_by_run:
        return {}
    items = (await db.scalars(select(Artifact).where(
        Artifact.run_id.in_(message_by_run), Artifact.owner_id == owner_id, Artifact.source == "generated",
        Artifact.state != "pending",
    ).order_by(Artifact.create_time, Artifact.id))).all()
    context_by_run = await load_run_context_artifact_ids(db, owner_id, list(message_by_run))
    read_sources = await load_read_source_artifact_ids(db, owner_id, [item.id for item in items])
    now = shanghai_now(naive=True)
    by_message: dict[int, list[dict]] = {}
    for item in items:
        by_message.setdefault(message_by_run[item.run_id], []).append({
            "artifact_id": item.id, "filename": item.filename, "media_type": item.media_type,
            "byte_size": item.byte_size,
            "context_artifact_ids": context_by_run.get(item.run_id, []),
            "read_source_artifact_ids": read_sources.get(item.id, []),
            "available": item.state == "ready" and item.is_deleted == 0 and item.status == 1
            and (item.expires_at is None or item.expires_at > now),
        })
    return by_message
