# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:12:32
# @Author  : yangyuexiong
# @File    : agent_run_message.py

"""保存运行输入和最终可见回答。"""

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentRunMessage(CustomBaseModel):
    __table_name__ = "agent_run_message"
    __table_args__ = (
        UniqueConstraint("run_id", "role", name="uq_yp_agent_run_message_run_role"),
        {
            "comment": "智能体运行可见消息表",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="所属运行 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="消息所属用户 ID")
    role: Mapped[str] = mapped_column(String(16), nullable=False, comment="消息角色：user 或 assistant")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="用户输入或最终可见回答")
