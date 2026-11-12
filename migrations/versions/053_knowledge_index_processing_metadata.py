# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 22:44:18
# @Author  : yangyuexiong
# @File    : 053_knowledge_index_processing_metadata.py

"""按已发布索引固定文档解析摘要，隔离后台解析缓存更新。

Revision ID: 053
Revises: 052
"""

from alembic import op
import sqlalchemy as sa

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增索引摘要快照，旧索引保持空值直至新代成功发布。"""
    op.add_column(
        "yp_knowledge_index",
        sa.Column("processing_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'"), comment="按文档版本 ID 固定的已发布解析摘要"),
    )


def downgrade() -> None:
    """仅删除新索引摘要，不修改片段、原文或活动代。"""
    op.drop_column("yp_knowledge_index", "processing_metadata")
