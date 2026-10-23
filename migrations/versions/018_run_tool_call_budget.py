# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 02:56:32
# @Author  : yangyuexiong
# @File    : 018_run_tool_call_budget.py

"""为运行任务持久化工具调用次数上限与已开始次数。

Revision ID: 018
Revises: 017
"""

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有运行保留不限次数的行为，并从零开始记录工具调用。"""
    op.add_column(
        "yp_agent_run", sa.Column("max_tool_calls", sa.Integer(), nullable=True, comment="本次运行允许的工具调用次数上限"),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0", comment="已开始的工具调用次数"),
    )


def downgrade() -> None:
    """移除运行任务的工具调用次数配置与计数。"""
    op.drop_column("yp_agent_run", "tool_call_count")
    op.drop_column("yp_agent_run", "max_tool_calls")
