# -*- coding: utf-8 -*-
# @Time    : 2026-11-24 23:27:00
# @Author  : yangyuexiong
# @File    : agent_run_public_history.py

"""可从事件源重建的公开消息缓存，不包含检查点私有上下文。"""

from sqlalchemy import Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentRunPublicHistory(CustomBaseModel):
    __table_name__ = "agent_run_public_history"
    __table_args__ = (
        {
            "comment": "运行公开消息投影缓存",
        },
    )

    __enable_auto_id__ = False

    __enable_audit_columns__ = False

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="所属业务 Run")
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, comment="公开消息覆盖的源事件上界")
    projection_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="公开投影实现版本")
    messages: Mapped[list] = mapped_column(JSON, nullable=False, comment="仅从公开事件投影得到的协议消息")
