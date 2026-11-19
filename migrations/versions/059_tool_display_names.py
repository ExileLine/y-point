# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 10:40:40
# @Author  : yangyuexiong
# @File    : 059_tool_display_names.py

"""分离 Tool 显示名称与不可变内部调用标识。"""

from alembic import op
import sqlalchemy as sa

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """回填现有工具显示名，不改变模型调用标识及 MCP 远端函数。"""
    with op.batch_alter_table("yp_user_tool") as batch:
        batch.add_column(sa.Column("display_name", sa.String(128), nullable=True, comment="工具显示名称，为空时回退内部标识"))
        batch.create_unique_constraint("uq_yp_user_tool_owner_display_name", ["owner_id", "display_name"])
    op.execute(sa.text("UPDATE yp_user_tool SET display_name = name"))


def downgrade() -> None:
    """删除显示字段，工具执行定义及内部标识保持不变。"""
    with op.batch_alter_table("yp_user_tool") as batch:
        batch.drop_constraint("uq_yp_user_tool_owner_display_name", type_="unique")
        batch.drop_column("display_name")
