# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : 015_artifacts.py

"""建立私有文件元数据表。

Revision ID: 015
Revises: 014
"""

import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建不含外键约束的文件元数据表。"""
    op.create_table(
        "yp_artifact",
        sa.Column("id", sa.String(36), primary_key=True, comment="文件 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="文件所属用户 ID"),
        sa.Column("run_id", sa.String(36), comment="关联运行 ID"),
        sa.Column("filename", sa.String(255), nullable=False, comment="展示用原始文件名"),
        sa.Column("media_type", sa.String(128), nullable=False, comment="校验后的文件类型"),
        sa.Column("byte_size", sa.BigInteger(), nullable=False, comment="文件字节数"),
        sa.Column("sha256", sa.String(64), nullable=False, comment="文件 SHA-256 摘要"),
        sa.Column("storage_key", sa.String(80), nullable=False, unique=True, comment="随机私有存储键"),
        sa.Column("source", sa.String(16), nullable=False, comment="文件来源：upload 或 generated"),
        sa.Column("state", sa.String(16), nullable=False, comment="文件状态：ready 或 deleted"),
        sa.Column("expires_at", sa.DateTime(timezone=True), comment="文件到期时间"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="用户附件与运行产物表",
    )
    op.create_index("ix_yp_artifact_owner_id", "yp_artifact", ["owner_id"])
    op.create_index("ix_yp_artifact_run_id", "yp_artifact", ["run_id"])


def downgrade() -> None:
    """删除文件元数据表。"""
    op.drop_index("ix_yp_artifact_run_id", table_name="yp_artifact")
    op.drop_index("ix_yp_artifact_owner_id", table_name="yp_artifact")
    op.drop_table("yp_artifact")
