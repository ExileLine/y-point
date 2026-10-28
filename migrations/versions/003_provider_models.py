# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:44:28
# @Author  : yangyuexiong
# @File    : 003_provider_models.py

"""新增供应商连接和可选模型表。

Revision ID: 003
Revises: 002
"""

from alembic import op
import sqlalchemy as sa


revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def _audit_columns() -> list[sa.Column]:
    """返回与 CustomBaseModel 一致的审计字段。"""
    return [
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """创建无数据库外键的供应商连接与模型表。"""
    op.create_table(
        "yp_provider_connection",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("name", sa.String(128), nullable=False, comment="连接名称"),
        sa.Column("name_key", sa.String(128), nullable=False, comment="小写连接名称唯一键"),
        sa.Column("kind", sa.String(32), nullable=False, comment="供应商类型"),
        sa.Column("base_url", sa.String(512), nullable=False, comment="供应商接口根地址"),
        sa.Column("encrypted_credentials", sa.Text(), comment="加密的 API 密钥和自定义请求头"),
        sa.Column("has_api_key", sa.Boolean(), nullable=False, comment="是否配置 API 密钥"),
        sa.Column("has_custom_headers", sa.Boolean(), nullable=False, comment="是否配置自定义请求头"),
        sa.Column("api_version", sa.String(64), comment="供应商接口版本"),
        sa.Column("region", sa.String(64), comment="供应商区域"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False, comment="请求超时秒数"),
        sa.Column("remark", sa.String(512), comment="备注"),
        sa.Column("creator_id", sa.Integer(), nullable=False, comment="创建人 ID"),
        *_audit_columns(),
        sa.UniqueConstraint("name_key", name="uq_yp_provider_connection_name_key"),
        comment="模型供应商连接表",
    )
    op.create_table(
        "yp_model_catalog",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("provider_id", sa.Integer(), nullable=False, comment="所属供应商连接 ID"),
        sa.Column("external_id", sa.String(255), nullable=False, comment="供应商模型 ID"),
        sa.Column("display_name", sa.String(128), nullable=False, comment="模型显示名称"),
        sa.Column("capabilities", sa.JSON(), nullable=False, comment="模型能力标签"),
        sa.Column("capabilities_verified", sa.Boolean(), nullable=False, comment="能力是否已实际验证"),
        sa.Column("context_window", sa.Integer(), comment="上下文窗口 Token 数"),
        sa.Column("input_price", sa.Numeric(14, 6), comment="每百万输入 Token 价格"),
        sa.Column("output_price", sa.Numeric(14, 6), comment="每百万输出 Token 价格"),
        sa.Column("price_currency", sa.String(3), nullable=False, comment="价格币种 ISO 4217 代码"),
        sa.Column("is_default", sa.Boolean(), nullable=False, comment="是否为默认模型"),
        sa.Column("creator_id", sa.Integer(), nullable=False, comment="创建人 ID"),
        *_audit_columns(),
        sa.UniqueConstraint("provider_id", "external_id", name="uq_yp_model_catalog_provider_external"),
        comment="可选模型表",
    )
    op.create_index("ix_yp_model_catalog_provider_id", "yp_model_catalog", ["provider_id"])


def downgrade() -> None:
    """删除可选模型和供应商连接表。"""
    op.drop_index("ix_yp_model_catalog_provider_id", table_name="yp_model_catalog")
    op.drop_table("yp_model_catalog")
    op.drop_table("yp_provider_connection")
