# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 14:43:17
# @Author  : yangyuexiong
# @File    : model_call_record.py

from decimal import Decimal

from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class ModelCallRecord(CustomBaseModel):
    __table_name__ = "model_call_record"
    __table_args__ = {"comment": "模型调用诊断记录表"}

    actor_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="发起调用的用户 ID")
    provider_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="调用时的供应商连接 ID")
    model_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="调用时的模型 ID")
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, comment="调用时的供应商模型 ID")
    call_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="调用类型")
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="调用是否成功")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="调用耗时毫秒数")
    upstream_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上游 HTTP 状态码")
    failure: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="脱敏后的故障类别")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上游报告的输入 Token 数")
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上游报告的输出 Token 数")
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(24, 12), nullable=True, comment="估算调用成本")
    price_currency: Mapped[str | None] = mapped_column(String(3), nullable=True, comment="估算成本币种")
