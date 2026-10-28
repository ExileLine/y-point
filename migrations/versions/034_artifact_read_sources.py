# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 09:08:24
# @Author  : yangyuexiong
# @File    : 034_artifact_read_sources.py

"""记录运行成功读取的旧产物及生成产物来源关系。

Revision ID: 034
Revises: 033
"""

import sqlalchemy as sa
from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """应用本版迁移：记录运行成功读取的旧产物及生成产物来源关系。"""
    op.create_table(
        "yp_run_artifact_read",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="读取发生的运行 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="运行所有者 ID"),
        sa.Column("source_artifact_id", sa.String(36), nullable=False, comment="已读取的旧产物 ID"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="运行已读取的旧生成产物",
    )
    op.create_index("ix_yp_run_artifact_read_run_id", "yp_run_artifact_read", ["run_id"])
    op.create_table(
        "yp_artifact_source",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("target_artifact_id", sa.String(36), nullable=False, comment="新产物 ID"),
        sa.Column("source_artifact_id", sa.String(36), nullable=False, comment="旧产物 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="生成运行 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="产物所有者 ID"),
        sa.Column("position", sa.Integer(), nullable=False, comment="首次读取顺序"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        sa.UniqueConstraint("target_artifact_id", "source_artifact_id", name="uq_yp_artifact_source_pair"),
        sa.UniqueConstraint("target_artifact_id", "position", name="uq_yp_artifact_source_position"),
        comment="模型生成文件与此前读取的旧产物关系",
    )
    op.create_index("ix_yp_artifact_source_target_artifact_id", "yp_artifact_source", ["target_artifact_id"])
    op.create_index("ix_yp_artifact_source_run_id", "yp_artifact_source", ["run_id"])


def downgrade() -> None:
    """回退本版迁移：记录运行成功读取的旧产物及生成产物来源关系。"""
    op.drop_index("ix_yp_artifact_source_run_id", table_name="yp_artifact_source")
    op.drop_index("ix_yp_artifact_source_target_artifact_id", table_name="yp_artifact_source")
    op.drop_table("yp_artifact_source")
    op.drop_index("ix_yp_run_artifact_read_run_id", table_name="yp_run_artifact_read")
    op.drop_table("yp_run_artifact_read")
