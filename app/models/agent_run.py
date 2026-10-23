# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:58:12
# @Author  : yangyuexiong
# @File    : agent_run.py

"""持久化运行任务及其调度状态。"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentRun(CustomBaseModel):
    __table_name__ = "agent_run"
    __enable_auto_id__ = False
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_yp_agent_run_owner_request_key"),
        {"comment": "智能体运行任务表"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="运行 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="运行所属用户 ID")
    agent_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="关联智能体 ID")
    model_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="运行时模型 ID")
    model_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120", comment="模型生成超时秒数",
    )
    thread_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="检查点线程 ID")
    request_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="用户提交幂等键")
    state: Mapped[str] = mapped_column(String(32), nullable=False, comment="运行状态")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="并发更新版本号")
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="最新事件序号")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="执行器租约令牌")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="执行器租约到期时间")
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="执行器最近心跳时间")
    checkpoint_id: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="最近持久化检查点 ID")
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="脱敏运行故障类别")
