# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:12:32
# @Author  : yangyuexiong
# @File    : 009_agent_run_messages.py

"""创建运行输入与最终回答表。

Revision ID: 009
Revises: 008
"""

import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增不使用外键的可见消息表。"""
    op.create_table(
        "yp_agent_run_message",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="所属运行 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="消息所属用户 ID"),
        sa.Column("role", sa.String(16), nullable=False, comment="消息角色：user 或 assistant"),
        sa.Column("content", sa.Text(), nullable=False, comment="用户输入或最终可见回答"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        sa.UniqueConstraint("run_id", "role", name="uq_yp_agent_run_message_run_role"),
        comment="智能体运行可见消息表",
    )
    op.create_index("ix_yp_agent_run_message_run_id", "yp_agent_run_message", ["run_id"])
    op.create_index("ix_yp_agent_run_message_owner_id", "yp_agent_run_message", ["owner_id"])


def downgrade() -> None:
    """删除运行可见消息表。"""
    op.drop_index("ix_yp_agent_run_message_owner_id", table_name="yp_agent_run_message")
    op.drop_index("ix_yp_agent_run_message_run_id", table_name="yp_agent_run_message")
    op.drop_table("yp_agent_run_message")
