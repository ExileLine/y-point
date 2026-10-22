# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 14:43:17
# @Author  : yangyuexiong
# @File    : 004_model_call_records.py

"""新增模型调用诊断记录表。

Revision ID: 004
Revises: 003
"""

from alembic import op
import sqlalchemy as sa


revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建不含外键、凭据和对话正文的模型调用记录表。"""
    op.create_table(
        "yp_model_call_record",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="ID"),
        sa.Column("actor_id", sa.Integer(), nullable=False, comment="发起调用的用户 ID"),
        sa.Column("provider_id", sa.Integer(), nullable=False, comment="调用时的供应商连接 ID"),
        sa.Column("model_id", sa.Integer(), nullable=False, comment="调用时的模型 ID"),
        sa.Column("external_id", sa.String(255), nullable=False, comment="调用时的供应商模型 ID"),
        sa.Column("call_type", sa.String(32), nullable=False, comment="调用类型"),
        sa.Column("available", sa.Boolean(), nullable=False, comment="调用是否成功"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, comment="调用耗时毫秒数"),
        sa.Column("upstream_status_code", sa.Integer(), comment="上游 HTTP 状态码"),
        sa.Column("failure", sa.String(32), comment="脱敏后的故障类别"),
        sa.Column("input_tokens", sa.Integer(), comment="上游报告的输入 Token 数"),
        sa.Column("output_tokens", sa.Integer(), comment="上游报告的输出 Token 数"),
        sa.Column("estimated_cost", sa.Numeric(24, 12), comment="估算调用成本"),
        sa.Column("price_currency", sa.String(3), comment="估算成本币种"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="模型调用诊断记录表",
    )
    op.create_index("ix_yp_model_call_record_actor_id", "yp_model_call_record", ["actor_id"])
    op.create_index("ix_yp_model_call_record_provider_id", "yp_model_call_record", ["provider_id"])
    op.create_index("ix_yp_model_call_record_model_id", "yp_model_call_record", ["model_id"])


def downgrade() -> None:
    """删除模型调用诊断记录表。"""
    op.drop_index("ix_yp_model_call_record_model_id", table_name="yp_model_call_record")
    op.drop_index("ix_yp_model_call_record_provider_id", table_name="yp_model_call_record")
    op.drop_index("ix_yp_model_call_record_actor_id", table_name="yp_model_call_record")
    op.drop_table("yp_model_call_record")
