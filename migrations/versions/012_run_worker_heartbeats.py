# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 10:02:24
# @Author  : yangyuexiong
# @File    : 012_run_worker_heartbeats.py

"""创建运行执行器心跳表。

Revision ID: 012
Revises: 011
"""

import sqlalchemy as sa
from alembic import op

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增不使用外键约束的执行器心跳记录。"""
    op.create_table(
        "yp_run_worker_heartbeat",
        sa.Column("id", sa.String(36), primary_key=True, comment="执行器启动实例 ID"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, comment="执行器启动时间"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, comment="最近心跳时间"),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True, comment="正常退出时间"),
        comment="运行执行器心跳表",
    )
    op.create_index("ix_yp_run_worker_heartbeat_last_seen_at", "yp_run_worker_heartbeat", ["last_seen_at"])


def downgrade() -> None:
    """删除执行器心跳记录。"""
    op.drop_index("ix_yp_run_worker_heartbeat_last_seen_at", table_name="yp_run_worker_heartbeat")
    op.drop_table("yp_run_worker_heartbeat")
