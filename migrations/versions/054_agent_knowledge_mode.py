# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 00:20:41
# @Author  : yangyuexiong
# @File    : 054_agent_knowledge_mode.py

"""增加知识回答模式与运行快照，历史记录默认采用智能融合。

Revision ID: 054
Revises: 053
"""

from alembic import op
import sqlalchemy as sa

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为智能体及运行增加非空模式，不改写既有会话、状态和知识索引。"""
    op.add_column(
        "yp_agent_config",
        sa.Column(
            "knowledge_mode",
            sa.String(16),
            nullable=False,
            server_default="auto",
            comment="知识回答模式：auto 智能融合，knowledge_only 仅知识库",
        ),
    )
    op.add_column(
        "yp_agent_run",
        sa.Column("agent_knowledge_mode", sa.String(16), nullable=False, server_default="auto", comment="提交时知识回答模式快照"),
    )


def downgrade() -> None:
    """移除回答模式，保留原有业务字段和历史数据。"""
    for table, column in (("yp_agent_run", "agent_knowledge_mode"), ("yp_agent_config", "knowledge_mode")):
        with op.batch_alter_table(table) as batch:
            batch.drop_column(column)
