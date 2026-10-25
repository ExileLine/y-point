# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:09:39
# @Author  : yangyuexiong
# @File    : 023_run_queue_timeout.py

"""为运行任务增加可选的排队等待期限。

Revision ID: 023
Revises: 022
"""

import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧运行保持不限制排队时间，新运行可保存固定截止时间。"""
    op.add_column("yp_agent_run", sa.Column("max_queue_wait_seconds", sa.Integer(), nullable=True, comment="排队等待最长秒数"))
    op.add_column("yp_agent_run", sa.Column("queue_expires_at", sa.DateTime(timezone=True), nullable=True, comment="排队等待截止时间"))
    op.create_index("ix_yp_agent_run_queue_expires_at", "yp_agent_run", ["queue_expires_at"])


def downgrade() -> None:
    """移除排队等待期限。"""
    op.drop_index("ix_yp_agent_run_queue_expires_at", table_name="yp_agent_run")
    op.drop_column("yp_agent_run", "queue_expires_at")
    op.drop_column("yp_agent_run", "max_queue_wait_seconds")
