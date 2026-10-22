# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 14:47:32
# @Author  : yangyuexiong
# @File    : 005_unique_default_model.py

"""限制全局默认模型只能有一个。

Revision ID: 005
Revises: 004
"""

from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加唯一默认占位键，并规范已有默认标记。"""
    with op.batch_alter_table("yp_model_catalog") as batch_op:
        batch_op.add_column(sa.Column("default_slot", sa.String(16), nullable=True, comment="全局默认模型占位键"))
        batch_op.create_unique_constraint("uq_yp_model_catalog_default_slot", ["default_slot"])
    model_table = sa.table(
        "yp_model_catalog", sa.column("id", sa.Integer), sa.column("is_default", sa.Boolean), sa.column("default_slot", sa.String)
    )
    connection = op.get_bind()
    default_ids = connection.execute(
        sa.select(model_table.c.id).where(model_table.c.is_default.is_(True)).order_by(model_table.c.id.desc())
    ).scalars().all()
    if default_ids:
        connection.execute(sa.update(model_table).where(model_table.c.id == default_ids[0]).values(default_slot="global"))
        for model_id in default_ids[1:]:
            connection.execute(sa.update(model_table).where(model_table.c.id == model_id).values(is_default=False))


def downgrade() -> None:
    """移除默认占位键及其唯一约束。"""
    with op.batch_alter_table("yp_model_catalog") as batch_op:
        batch_op.drop_constraint("uq_yp_model_catalog_default_slot", type_="unique")
        batch_op.drop_column("default_slot")
