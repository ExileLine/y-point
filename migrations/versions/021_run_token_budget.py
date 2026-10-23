# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 03:15:38
# @Author  : yangyuexiong
# @File    : 021_run_token_budget.py

"""为运行任务持久化上游报告的 Token 预算与累计用量。

Revision ID: 021
Revises: 020
"""

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有运行保留不限制 Token 的行为，并从零累计新调用用量。"""
    op.add_column("yp_agent_run", sa.Column("max_total_tokens", sa.Integer(), nullable=True, comment="本次运行允许的上游总 Token 上限"))
    op.add_column(
        "yp_agent_run",
        sa.Column("reported_total_tokens", sa.Integer(), nullable=False, server_default="0", comment="本次运行上游已报告的总 Token 数"),
    )


def downgrade() -> None:
    """移除上游 Token 预算与累计用量。"""
    op.drop_column("yp_agent_run", "reported_total_tokens")
    op.drop_column("yp_agent_run", "max_total_tokens")
