# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:42:00
# @Author  : yangyuexiong
# @File    : 032_attachment_context.py
"""记录哪些消息附件进入模型上下文。

Revision ID: 032
Revises: 031
"""

import sqlalchemy as sa
from alembic import op

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为历史附件补充默认为关闭的模型上下文标记。"""
    op.add_column(
        "yp_agent_message_attachment",
        sa.Column("included_in_context", sa.Boolean(), nullable=False, server_default=sa.false(), comment="是否将文本内容送入本轮模型上下文"),
    )


def downgrade() -> None:
    """移除模型上下文标记。"""
    op.drop_column("yp_agent_message_attachment", "included_in_context")
