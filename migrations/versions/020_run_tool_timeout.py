# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 03:08:56
# @Author  : yangyuexiong
# @File    : 020_run_tool_timeout.py

"""为运行任务持久化单次工具执行时限。

Revision ID: 020
Revises: 019
"""

import sqlalchemy as sa
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有和新建运行设置默认 120 秒单次工具时限。"""
    op.add_column(
        "yp_agent_run",
        sa.Column("tool_timeout_seconds", sa.Integer(), nullable=False, server_default="120", comment="单次工具执行超时秒数"),
    )


def downgrade() -> None:
    """移除单次工具执行时限。"""
    op.drop_column("yp_agent_run", "tool_timeout_seconds")
