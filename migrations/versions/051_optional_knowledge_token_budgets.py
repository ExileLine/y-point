# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 18:07:52
# @Author  : yangyuexiong
# @File    : 051_optional_knowledge_token_budgets.py

"""允许知识加工和索引嵌入不限制累计 Token。

Revision ID: 051
Revises: 050
"""

from alembic import op
import sqlalchemy as sa

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """放宽预算可空性，移除加工默认额度，保留现有数值及任务快照。"""
    with op.batch_alter_table("yp_knowledge_base") as batch:
        batch.alter_column(
            "embedding_token_budget",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
            existing_comment="单次构建的嵌入输入预算",
            comment="单次构建的嵌入输入预算，空值不限制累计 Token",
        )
        batch.alter_column(
            "processing_token_budget",
            existing_type=sa.Integer(),
            existing_nullable=False,
            nullable=True,
            existing_server_default="100000",
            server_default=None,
            existing_comment="单次构建跨重试的加工预留 Token 上限",
            comment="单次构建跨重试的加工预留 Token 上限，空值不限制累计 Token",
        )


def downgrade() -> None:
    """仅为空预算回填旧默认值，再恢复非空约束及旧加工默认额度。"""
    table = sa.table(
        "yp_knowledge_base",
        sa.column("embedding_token_budget", sa.Integer()),
        sa.column("processing_token_budget", sa.Integer()),
    )
    op.execute(table.update().where(table.c.embedding_token_budget.is_(None)).values(embedding_token_budget=200000))
    op.execute(table.update().where(table.c.processing_token_budget.is_(None)).values(processing_token_budget=100000))
    with op.batch_alter_table("yp_knowledge_base") as batch:
        batch.alter_column(
            "embedding_token_budget",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
            existing_comment="单次构建的嵌入输入预算，空值不限制累计 Token",
            comment="单次构建的嵌入输入预算",
        )
        batch.alter_column(
            "processing_token_budget",
            existing_type=sa.Integer(),
            existing_nullable=True,
            nullable=False,
            server_default="100000",
            existing_comment="单次构建跨重试的加工预留 Token 上限，空值不限制累计 Token",
            comment="单次构建跨重试的加工预留 Token 上限",
        )
