# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 10:35:20
# @Author  : yangyuexiong
# @File    : 013_runtime_model_calls.py

"""为模型调用记录增加运行关联。

Revision ID: 013
Revises: 012
"""

import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为运行时模型调用增加不带外键约束的 Run ID。"""
    op.add_column("yp_model_call_record", sa.Column("run_id", sa.String(36), nullable=True, comment="关联运行 ID"))
    op.create_index("ix_yp_model_call_record_run_id", "yp_model_call_record", ["run_id"])


def downgrade() -> None:
    """移除模型调用记录中的运行关联。"""
    op.drop_index("ix_yp_model_call_record_run_id", table_name="yp_model_call_record")
    op.drop_column("yp_model_call_record", "run_id")
