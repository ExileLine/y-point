# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : aps_task.py

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class ApsTask(CustomBaseModel):
    __table_name__ = "aps_tasks"
    __table_args__ = {"comment": "定时任务表"}

    task_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="任务id")
    trigger_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="触发器类型:date;interval;cron")
    trigger_param: Mapped[dict] = mapped_column(JSON, nullable=False, comment="触发器参数")
    task_function_name: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="任务函数名称")
    task_function_args: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="任务参数:args")
    task_function_kwargs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, comment="任务参数:kwargs")
