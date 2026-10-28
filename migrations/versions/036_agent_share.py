# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 14:25:00
# @Author  : yangyuexiong
# @File    : 036_agent_share.py

"""新增 Agent 指定用户授权。

Revision ID: 036
Revises: 035
"""

import sqlalchemy as sa
from alembic import op

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """应用本版迁移：新增 Agent 指定用户授权。"""
    op.create_table(
        "yp_agent_share",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="被共享智能体 ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="获授权用户 ID"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        sa.UniqueConstraint("agent_id", "user_id", name="uq_yp_agent_share_agent_user"),
        comment="智能体指定用户授权表",
    )
    op.create_index("ix_yp_agent_share_agent_id", "yp_agent_share", ["agent_id"])
    op.create_index("ix_yp_agent_share_user_id", "yp_agent_share", ["user_id"])


def downgrade() -> None:
    """回退本版迁移：新增 Agent 指定用户授权。"""
    op.drop_index("ix_yp_agent_share_user_id", table_name="yp_agent_share")
    op.drop_index("ix_yp_agent_share_agent_id", table_name="yp_agent_share")
    op.drop_table("yp_agent_share")
