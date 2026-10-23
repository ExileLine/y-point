# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 02:31:43
# @Author  : yangyuexiong
# @File    : 017_run_model_call_budget.py

"""为运行任务持久化模型调用次数上限与已开始次数。

Revision ID: 017
Revises: 016
"""

import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有运行保留不限次数的行为，并从零开始记录新调用。"""
    op.add_column(
        "yp_agent_run", sa.Column("max_model_calls", sa.Integer(), nullable=True, comment="本次运行允许的模型调用次数上限"),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("model_call_count", sa.Integer(), nullable=False, server_default="0", comment="已开始的模型调用次数"),
    )


def downgrade() -> None:
    """移除运行任务的模型调用次数配置与计数。"""
    op.drop_column("yp_agent_run", "model_call_count")
    op.drop_column("yp_agent_run", "max_model_calls")
