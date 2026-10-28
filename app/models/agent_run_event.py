# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:58:12
# @Author  : yangyuexiong
# @File    : agent_run_event.py

"""保存可按游标回放的运行状态事件。"""

from sqlalchemy import Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentRunEvent(CustomBaseModel):
    __table_name__ = "agent_run_event"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_yp_agent_run_event_run_sequence"),
        {
            "comment": "智能体运行事件表",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="所属运行 ID")
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, comment="运行内单调事件序号")
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="事件协议版本")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="事件类型")
    state: Mapped[str] = mapped_column(String(32), nullable=False, comment="事件发生后的运行状态")
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="脱敏运行故障类别")
    content: Mapped[str | None] = mapped_column(Text, nullable=True, comment="可公开的模型文本增量")
    tool_call_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="服务端生成的工具调用关联 ID")
    tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="已授权工具名称")
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="已发布生成产物 ID")
