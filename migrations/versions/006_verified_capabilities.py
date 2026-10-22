# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 16:14:42
# @Author  : yangyuexiong
# @File    : 006_verified_capabilities.py

"""保存逐项验证的模型能力。

Revision ID: 006
Revises: 005
"""

from alembic import op
import sqlalchemy as sa


revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增能力验证列表，已有模型默认没有验证证据。"""
    with op.batch_alter_table("yp_model_catalog") as batch_op:
        batch_op.add_column(sa.Column("verified_capabilities", sa.JSON(), nullable=True, comment="实际探测通过的模型能力"))
    op.execute(sa.text("UPDATE yp_model_catalog SET verified_capabilities = '[]' WHERE verified_capabilities IS NULL"))
    with op.batch_alter_table("yp_model_catalog") as batch_op:
        batch_op.alter_column(
            "verified_capabilities", existing_type=sa.JSON(), nullable=False,
            existing_comment="实际探测通过的模型能力",
        )


def downgrade() -> None:
    """删除逐项能力验证列表。"""
    with op.batch_alter_table("yp_model_catalog") as batch_op:
        batch_op.drop_column("verified_capabilities")
