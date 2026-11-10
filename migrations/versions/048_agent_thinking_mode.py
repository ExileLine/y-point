# -*- coding: utf-8 -*-
# @Time    : 2026-11-09 23:32:33
# @Author  : yangyuexiong
# @File    : 048_agent_thinking_mode.py

"""为智能体及运行快照增加可显式配置的模型思考模式。

Revision ID: 048
Revises: 047
"""

from alembic import op
import sqlalchemy as sa

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史配置和运行采用跟随上游模式，不改写既有额度、状态和超时。"""
    op.add_column(
        "yp_agent_config",
        sa.Column(
            "thinking_mode",
            sa.String(16),
            nullable=False,
            server_default="auto",
            comment="模型思考模式：auto 跟随上游，enabled 启用，disabled 关闭",
        ),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("agent_thinking_mode", sa.String(16), nullable=False, server_default="auto", comment="提交时模型思考模式快照"),
    )


def downgrade() -> None:
    """移除思考偏好字段，保留其余配置与历史运行。"""
    for table, column in (("yp_agent_run", "agent_thinking_mode"), ("yp_agent_config", "thinking_mode")):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
