# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 11:00:31
# @Author  : yangyuexiong
# @File    : agent_config.py

"""用户私有 Agent 的可编辑配置。"""

from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentConfig(CustomBaseModel):
    __table_name__ = "agent_config"
    __table_args__ = {"comment": "用户智能体配置表"}

    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="智能体所属用户 ID")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="智能体名称")
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="智能体描述")
    icon: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="智能体图标标识")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="", comment="系统指令")
    welcome_message: Mapped[str | None] = mapped_column(String(2048), nullable=True, comment="欢迎语")
    default_model_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="默认模型 ID")
    temperature: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False, default=Decimal("1"), comment="生成温度")
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096, comment="单次最大输出 Token 数")
    model_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120", comment="单次模型生成超时秒数",
    )
    tool_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=120, server_default="120", comment="单次工具执行超时秒数",
    )
    max_model_calls: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每轮模型调用次数上限")
    max_tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每轮工具调用次数上限")
    max_total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="每轮上游报告的总 Token 上限")
    max_estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True, comment="每轮估算成本上限")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="配置版本号")
