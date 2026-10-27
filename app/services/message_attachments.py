# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:35:00
# @Author  : yangyuexiong
# @File    : message_attachments.py
"""批量读取消息中的上传文件引用及当前可用状态。"""

import hashlib
import json

from boto3.exceptions import Boto3Error
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run_message import AgentRunMessage
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.services.artifact_store import artifact_root
from app.services.private_blob_store import read_blob

MAX_CONTEXT_ATTACHMENT_BYTES = 16 * 1024


class AttachmentContextUnavailable(ValueError):
    """已选择的模型上下文附件在执行前失效或内容不匹配。"""


async def load_model_attachment_context(db: AsyncSession, run_id: str, owner_id: int) -> str | None:
    """按提交顺序验证并读取本轮选择的 TXT；不把原文写入公开消息。"""
    references = (await db.scalars(select(AgentMessageAttachment).join(
        AgentRunMessage, AgentRunMessage.id == AgentMessageAttachment.message_id,
    ).where(
        AgentRunMessage.run_id == run_id, AgentRunMessage.owner_id == owner_id,
        AgentRunMessage.role == "user", AgentMessageAttachment.included_in_context.is_(True),
    ).order_by(AgentMessageAttachment.position))).all()
    if not references:
        return None
    if len(references) > 5 or sum(item.byte_size for item in references) > MAX_CONTEXT_ATTACHMENT_BYTES:
        raise AttachmentContextUnavailable("模型上下文附件超出限制")
    items = (await db.scalars(select(Artifact).where(
        Artifact.id.in_([item.artifact_id for item in references]), Artifact.owner_id == owner_id,
        Artifact.source == "upload", Artifact.run_id.is_(None), Artifact.state == "ready",
        Artifact.status == 1, Artifact.is_deleted == 0, Artifact.media_type == "text/plain",
        (Artifact.expires_at.is_(None) | (Artifact.expires_at > shanghai_now(naive=True))),
    ))).all()
    by_id = {item.id: item for item in items}
    if len(by_id) != len(references):
        raise AttachmentContextUnavailable("模型上下文附件不可用")
    chunks = []
    for reference in references:
        item = by_id[reference.artifact_id]
        if item.byte_size != reference.byte_size or item.filename != reference.filename:
            raise AttachmentContextUnavailable("模型上下文附件已变化")
        try:
            content = await read_blob(artifact_root(), item.storage_key, max_bytes=MAX_CONTEXT_ATTACHMENT_BYTES)
            if len(content) != item.byte_size or hashlib.sha256(content).hexdigest() != item.sha256:
                raise AttachmentContextUnavailable("模型上下文附件校验失败")
            decoded = content.decode("utf-8")
        except (OSError, UnicodeError, ValueError, Boto3Error, BotoCoreError, ClientError):
            raise AttachmentContextUnavailable("模型上下文附件不可读取") from None
        chunks.append(json.dumps({"filename": reference.filename, "content": decoded}, ensure_ascii=False))
    return "\n".join(chunks)


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
    now = shanghai_now(naive=True)
    by_message: dict[int, list[dict]] = {}
    for item in items:
        by_message.setdefault(message_by_run[item.run_id], []).append({
            "artifact_id": item.id, "filename": item.filename, "media_type": item.media_type,
            "byte_size": item.byte_size,
            "available": item.state == "ready" and item.is_deleted == 0 and item.status == 1
            and (item.expires_at is None or item.expires_at > now),
        })
    return by_message
