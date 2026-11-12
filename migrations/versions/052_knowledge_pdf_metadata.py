# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 22:11:26
# @Author  : yangyuexiong
# @File    : 052_knowledge_pdf_metadata.py

"""保存 PDF 页面分类、OCR 质量和布局摘要。

Revision ID: 052
Revises: 051
"""

from alembic import op
import sqlalchemy as sa

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增解析摘要，旧版本为空摘要且保留原文和已发布索引。"""
    op.add_column(
        "yp_knowledge_document_version",
        sa.Column("processing_metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'"), comment="文档解析摘要及 PDF 逐页分类、OCR 质量与布局提示"),
    )


def downgrade() -> None:
    """仅删除新增摘要，保留原始文档、片段及活动索引。"""
    op.drop_column("yp_knowledge_document_version", "processing_metadata")
