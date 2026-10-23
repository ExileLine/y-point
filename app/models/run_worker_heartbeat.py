# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 10:02:24
# @Author  : yangyuexiong
# @File    : run_worker_heartbeat.py

"""持久化独立运行执行器的进程心跳。"""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class RunWorkerHeartbeat(CustomBaseModel):
    """记录一次 worker 启动周期的最后存活时间。"""

    __table_name__ = "run_worker_heartbeat"
    __enable_auto_id__ = False
    __enable_audit_columns__ = False
    __table_args__ = {"comment": "运行执行器心跳表"}

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="执行器启动实例 ID")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="执行器启动时间")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True, comment="最近心跳时间")
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="正常退出时间")
