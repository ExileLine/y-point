# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:54:35
# @Author  : yangyuexiong
# @File    : 055_align_runtime_column_comments.py

"""同步运行时字段中文注释，不改变类型、约束或业务数据。

Revision ID: 055
Revises: 054
"""

from alembic import op
import sqlalchemy as sa

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None


def _update_comments(upgrading: bool) -> None:
    """仅在支持列注释的数据库同步当前或历史文案，SQLite 不重建业务表。"""
    if op.get_bind().dialect.name == "sqlite":
        return
    for table, column, nullable, old, current in (
        ("yp_agent_run", "model_timeout_seconds", False, "模型生成超时秒数", "单次模型生成超时秒数"),
        ("yp_user_tool", "skill_id", True, "所属 Skill ID，非空表示包内托管脚本", "所属 Skill ID，空表示独立 Tool"),
    ):
        op.alter_column(
            table,
            column,
            existing_type=sa.Integer(),
            existing_nullable=nullable,
            existing_comment=old if upgrading else current,
            comment=current if upgrading else old,
        )


def upgrade() -> None:
    """将两处历史列注释对齐当前 ORM，保留所有业务值。"""
    _update_comments(True)


def downgrade() -> None:
    """恢复两处历史列注释，保持类型及业务值不变。"""
    _update_comments(False)
