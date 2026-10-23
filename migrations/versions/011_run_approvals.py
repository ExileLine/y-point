# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:49:44
# @Author  : yangyuexiong
# @File    : 011_run_approvals.py

"""创建运行审批请求表。

Revision ID: 011
Revises: 010
"""

import sqlalchemy as sa
from alembic import op

revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增不使用外键约束的运行审批记录。"""
    op.create_table(
        "yp_run_approval",
        sa.Column("id", sa.String(36), primary_key=True, comment="审批请求 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="所属运行 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="审批所属用户 ID"),
        sa.Column("checkpoint_id", sa.String(128), nullable=False, comment="审批中断对应的检查点 ID"),
        sa.Column("actions", sa.JSON(), nullable=False, comment="待审批工具名与参数摘要列表"),
        sa.Column("state", sa.String(16), nullable=False, comment="审批状态：pending、approved、rejected、expired 或 cancelled"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="审批到期时间"),
        sa.Column("decided_by", sa.Integer(), nullable=True, comment="作出审批决定的用户 ID"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True, comment="审批决定时间"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        sa.UniqueConstraint("run_id", "checkpoint_id", name="uq_yp_run_approval_run_checkpoint"),
        comment="运行人工审批请求表",
    )
    op.create_index("ix_yp_run_approval_run_id", "yp_run_approval", ["run_id"])
    op.create_index("ix_yp_run_approval_owner_id", "yp_run_approval", ["owner_id"])


def downgrade() -> None:
    """删除运行审批记录。"""
    op.drop_index("ix_yp_run_approval_owner_id", table_name="yp_run_approval")
    op.drop_index("ix_yp_run_approval_run_id", table_name="yp_run_approval")
    op.drop_table("yp_run_approval")
