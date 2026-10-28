# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:35:00
# @Author  : yangyuexiong
# @File    : agent_message_attachment.py
"""会话用户消息引用的已上传私有文件。"""

from sqlalchemy import BigInteger, Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentMessageAttachment(CustomBaseModel):
    __table_name__ = "agent_message_attachment"
    __table_args__ = (
        UniqueConstraint("message_id", "artifact_id", name="uq_yp_agent_message_attachment_message_artifact"),
        UniqueConstraint("message_id", "position", name="uq_yp_agent_message_attachment_message_position"),
        {"comment": "会话用户消息的上传文件引用"},
    )

    message_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属消息 ID")
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="上传文件 ID")
    position: Mapped[int] = mapped_column(Integer, nullable=False, comment="消息内展示顺序")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, comment="提交时文件名快照")
    media_type: Mapped[str] = mapped_column(String(128), nullable=False, comment="提交时文件类型快照")
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="提交时文件大小快照")
    included_in_context: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否将文本内容送入本轮模型上下文")
