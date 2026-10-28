# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:49:44
# @Author  : yangyuexiong
# @File    : run_approval.py

"""持久化运行中断后的人工审批请求和决定。"""

from datetime import datetime

from sqlalchemy import Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel, ShanghaiDateTime


class RunApproval(CustomBaseModel):
    __table_name__ = "run_approval"
    __enable_auto_id__ = False
    __table_args__ = (
        UniqueConstraint("run_id", "checkpoint_id", name="uq_yp_run_approval_run_checkpoint"),
        {
            "comment": "运行人工审批请求表",
        },
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="审批请求 ID")
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="所属运行 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="审批所属用户 ID")
    checkpoint_id: Mapped[str] = mapped_column(String(128), nullable=False, comment="审批中断对应的检查点 ID")
    actions: Mapped[list[dict]] = mapped_column(JSON, nullable=False, comment="待审批工具名与参数摘要列表")
    state: Mapped[str] = mapped_column(String(16), nullable=False, comment="审批状态：pending、approved、rejected、expired 或 cancelled")
    expires_at: Mapped[datetime] = mapped_column(ShanghaiDateTime(), nullable=False, comment="审批到期时间")
    decided_by: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="作出审批决定的用户 ID")
    decided_at: Mapped[datetime | None] = mapped_column(ShanghaiDateTime(), nullable=True, comment="审批决定时间")
