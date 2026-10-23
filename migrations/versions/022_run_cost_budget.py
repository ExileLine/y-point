# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 04:16:11
# @Author  : yangyuexiong
# @File    : 022_run_cost_budget.py

"""为运行任务持久化估算成本预算与价格快照。

Revision ID: 022
Revises: 021
"""

import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可选成本上限、累计成本和创建时的模型计价信息。"""
    op.add_column(
        "yp_agent_run", sa.Column("max_estimated_cost", sa.Numeric(18, 6), nullable=True, comment="本次运行估算成本上限"),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("estimated_cost", sa.Numeric(24, 12), nullable=False, server_default="0", comment="启用预算时的累计估算成本"),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("cost_input_price", sa.Numeric(14, 6), nullable=True, comment="创建运行时每百万输入 Token 价格"),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("cost_output_price", sa.Numeric(14, 6), nullable=True, comment="创建运行时每百万输出 Token 价格"),
    )
    op.add_column("yp_agent_run", sa.Column("cost_currency", sa.String(3), nullable=True, comment="创建运行时价格币种"))


def downgrade() -> None:
    """移除运行成本预算及其价格快照。"""
    op.drop_column("yp_agent_run", "cost_currency")
    op.drop_column("yp_agent_run", "cost_output_price")
    op.drop_column("yp_agent_run", "cost_input_price")
    op.drop_column("yp_agent_run", "estimated_cost")
    op.drop_column("yp_agent_run", "max_estimated_cost")
