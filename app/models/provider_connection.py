# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:44:28
# @Author  : yangyuexiong
# @File    : provider_connection.py

from sqlalchemy import Boolean, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class ProviderConnection(CustomBaseModel):
    __table_name__ = "provider_connection"
    __table_args__ = (
        UniqueConstraint("name_key", name="uq_yp_provider_connection_name_key"),
        {
            "comment": "模型供应商连接表",
        },
    )

    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="连接名称")
    name_key: Mapped[str] = mapped_column(String(128), nullable=False, comment="小写连接名称唯一键")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, comment="供应商类型")
    base_url: Mapped[str] = mapped_column(String(512), nullable=False, comment="供应商接口根地址")
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密的 API 密钥和自定义请求头")
    has_api_key: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否配置 API 密钥")
    has_custom_headers: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="是否配置自定义请求头")
    api_version: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="供应商接口版本")
    region: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="供应商区域")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=15, comment="请求超时秒数")
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="备注")
    creator_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="创建人 ID")
