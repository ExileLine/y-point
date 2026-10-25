# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:40:00
# @Author  : yangyuexiong
# @File    : 025_artifact_reference_event.py

"""记录生成产物来源及可回放的产物引用事件。

Revision ID: 025
Revises: 024
"""

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增无外键来源映射与事件引用。"""
    op.add_column("yp_artifact", sa.Column("source_artifact_id", sa.String(36), nullable=True, comment="生成产物的来源文件 ID"))
    op.add_column("yp_agent_run_event", sa.Column("artifact_id", sa.String(36), nullable=True, comment="已发布生成产物 ID"))


def downgrade() -> None:
    """移除产物引用字段。"""
    op.drop_column("yp_agent_run_event", "artifact_id")
    op.drop_column("yp_artifact", "source_artifact_id")
