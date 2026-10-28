# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 11:10:31
# @Author  : yangyuexiong
# @File    : 035_artifact_state_comment.py

"""同步文件暂存状态的数据库字段注释。

Revision ID: 035
Revises: 034
"""

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """将 PostgreSQL/MySQL 字段注释更新为当前状态集合。"""
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column(
            "yp_artifact", "state", existing_type=sa.String(16), existing_nullable=False,
            comment="文件状态：pending、ready 或 deleted",
        )


def downgrade() -> None:
    """恢复原字段注释。"""
    if op.get_bind().dialect.name != "sqlite":
        op.alter_column(
            "yp_artifact", "state", existing_type=sa.String(16), existing_nullable=False,
            comment="文件状态：ready 或 deleted",
        )
