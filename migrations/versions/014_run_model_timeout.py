# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 10:41:12
# @Author  : yangyuexiong
# @File    : 014_run_model_timeout.py

"""为运行任务持久化模型生成超时上限。

Revision ID: 014
Revises: 013
"""

import sqlalchemy as sa
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有和新建运行设置默认 120 秒模型生成上限。"""
    op.add_column(
        "yp_agent_run", sa.Column("model_timeout_seconds", sa.Integer(), nullable=False, server_default="120", comment="模型生成超时秒数")
    )


def downgrade() -> None:
    """移除运行任务的模型生成上限。"""
    op.drop_column("yp_agent_run", "model_timeout_seconds")
