# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 19:59:52
# @Author  : yangyuexiong
# @File    : 049_optional_output_tokens.py

"""允许 Agent 显式清空输出额度并跟随供应商。

Revision ID: 049
Revises: 048
"""

from alembic import op
import sqlalchemy as sa

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅放宽可空性并更新字段说明，保留历史 Agent 和运行快照。"""
    with op.batch_alter_table("yp_agent_config") as batch:
        batch.alter_column(
            "max_output_tokens",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
            existing_comment="单次最大输出 Token 数",
            comment="单次最大输出 Token 数，空值跟随供应商",
        )


def downgrade() -> None:
    """将新产生的空额度回填旧版默认值，再恢复非空约束，保留运行快照。"""
    table = sa.table("yp_agent_config", sa.column("max_output_tokens", sa.Integer()))
    op.execute(table.update().where(table.c.max_output_tokens.is_(None)).values(max_output_tokens=4096))
    with op.batch_alter_table("yp_agent_config") as batch:
        batch.alter_column(
            "max_output_tokens",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
            existing_comment="单次最大输出 Token 数，空值跟随供应商",
            comment="单次最大输出 Token 数",
        )
