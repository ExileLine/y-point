# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:44:28
# @Author  : yangyuexiong
# @File    : model_catalog.py

from decimal import Decimal
from datetime import datetime

from sqlalchemy import Boolean, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel, ShanghaiDateTime


class ModelCatalog(CustomBaseModel):
    __table_name__ = "model_catalog"
    __table_args__ = (
        UniqueConstraint("provider_id", "external_id", name="uq_yp_model_catalog_provider_external"),
        UniqueConstraint("default_slot", name="uq_yp_model_catalog_default_slot"),
        {
            "comment": "可选模型表",
        },
    )

    provider_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属供应商连接 ID")
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, comment="供应商模型 ID")
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模型显示名称")
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, comment="模型能力标签")
    verified_capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, comment="实际探测通过的模型能力")
    capabilities_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="能力是否已实际验证")
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="实际嵌入探测通过的向量维度")
    availability_failure: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="最近确认的模型访问故障，空值表示未被阻断")
    availability_checked_at: Mapped[datetime | None] = mapped_column(ShanghaiDateTime(), nullable=True, comment="最近模型访问状态核验时间")
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上下文窗口 Token 数")
    input_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True, comment="每百万输入 Token 价格")
    output_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True, comment="每百万输出 Token 价格")
    price_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD", comment="价格币种 ISO 4217 代码")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否为默认模型")
    default_slot: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="全局默认模型占位键")
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="创建人 ID")
