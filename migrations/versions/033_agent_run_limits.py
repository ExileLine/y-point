# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 23:09:00
# @Author  : yangyuexiong
# @File    : 033_agent_run_limits.py

"""为 Agent 增加每轮运行超时与预算配置。

Revision ID: 033
Revises: 032
"""

import sqlalchemy as sa
from alembic import op

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为已有 Agent 回填默认超时，预算默认不限制。"""
    for column in (
        sa.Column("model_timeout_seconds", sa.Integer(), nullable=False, server_default="120", comment="单次模型生成超时秒数"),
        sa.Column("tool_timeout_seconds", sa.Integer(), nullable=False, server_default="120", comment="单次工具执行超时秒数"),
        sa.Column("max_model_calls", sa.Integer(), nullable=True, comment="每轮模型调用次数上限"),
        sa.Column("max_tool_calls", sa.Integer(), nullable=True, comment="每轮工具调用次数上限"),
        sa.Column("max_total_tokens", sa.Integer(), nullable=True, comment="每轮上游报告的总 Token 上限"),
        sa.Column("max_estimated_cost", sa.Numeric(18, 6), nullable=True, comment="每轮估算成本上限"),
    ):
        op.add_column("yp_agent_config", column)


def downgrade() -> None:
    """移除 Agent 的运行超时与预算字段。"""
    for name in (
        "max_estimated_cost",
        "max_total_tokens",
        "max_tool_calls",
        "max_model_calls",
        "tool_timeout_seconds",
        "model_timeout_seconds",
    ):
        op.drop_column("yp_agent_config", name)
