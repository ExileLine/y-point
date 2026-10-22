# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:44:28
# @Author  : yangyuexiong
# @File    : model_catalog.py

from decimal import Decimal

from sqlalchemy import Boolean, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class ModelCatalog(CustomBaseModel):
    __table_name__ = "model_catalog"
    __table_args__ = (
        UniqueConstraint("provider_id", "external_id", name="uq_yp_model_catalog_provider_external"),
        {"comment": "可选模型表"},
    )

    provider_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属供应商连接 ID")
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, comment="供应商模型 ID")
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="模型显示名称")
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list, comment="模型能力标签")
    capabilities_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="能力是否已实际验证")
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="上下文窗口 Token 数")
    input_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True, comment="每百万输入 Token 价格")
    output_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 6), nullable=True, comment="每百万输出 Token 价格")
    price_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD", comment="价格币种 ISO 4217 代码")
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否为默认模型")
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="创建人 ID")
