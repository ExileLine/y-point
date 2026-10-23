# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:25:18
# @Author  : yangyuexiong
# @File    : 010_run_text_events.py

"""为运行事件增加可公开的文本增量。

Revision ID: 010
Revises: 009
"""

import sqlalchemy as sa
from alembic import op

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增模型可见文本片段列。"""
    with op.batch_alter_table("yp_agent_run_event") as batch_op:
        batch_op.add_column(sa.Column("content", sa.Text(), nullable=True, comment="可公开的模型文本增量"))


def downgrade() -> None:
    """删除模型可见文本片段列。"""
    with op.batch_alter_table("yp_agent_run_event") as batch_op:
        batch_op.drop_column("content")
