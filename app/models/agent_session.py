# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:23:43
# @Author  : yangyuexiong
# @File    : agent_session.py

"""私有 Agent 的对话会话、当前模型及执行线程。"""

from sqlalchemy import Boolean, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentSession(CustomBaseModel):
    __table_name__ = "agent_session"
    __table_args__ = (
        UniqueConstraint("thread_id", name="uq_yp_agent_session_thread_id"),
        {
            "comment": "智能体私有对话会话表",
        },
    )

    __enable_auto_id__ = False

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="会话 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="会话所属用户 ID")
    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="会话所属智能体 ID")
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="会话当前模型 ID；旧记录为空时从历史回填")
    thread_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="专属检查点线程 ID")
    title: Mapped[str] = mapped_column(String(128), nullable=False, comment="会话标题")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否已归档")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="会话并发更新版本号")
