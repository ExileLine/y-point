# -*- coding: utf-8 -*-
# @Time    : 2026-11-16
# @Author  : yangyuexiong
# @File    : 056_run_tool_approval_policy.py

"""独立固定每轮工具审批设置，避免 Agent 旧执行版本阻挡审批开关生效。

Revision ID: 056
Revises: 055
"""

from alembic import op
import sqlalchemy as sa

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新运行记录审批设置，历史空值继续采用原工具版本的审批策略。"""
    op.add_column(
        "yp_run_tool",
        sa.Column("requires_approval", sa.Boolean(), nullable=True, comment="提交时审批设置快照，空值沿用历史工具版本"),
    )


def downgrade() -> None:
    """移除独立审批快照，保留运行及不可变工具版本记录。"""
    with op.batch_alter_table("yp_run_tool") as batch:
        batch.drop_column("requires_approval")
