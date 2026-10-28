# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:35:00
# @Author  : yangyuexiong
# @File    : 031_agent_message_attachments.py
"""记录会话消息引用的上传文件。

Revision ID: 031
Revises: 030
"""

import sqlalchemy as sa
from alembic import op

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增无外键的消息附件关系表。"""
    op.create_table(
        "yp_agent_message_attachment",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("message_id", sa.Integer(), nullable=False, comment="所属消息 ID"),
        sa.Column("artifact_id", sa.String(36), nullable=False, comment="上传文件 ID"),
        sa.Column("position", sa.Integer(), nullable=False, comment="消息内展示顺序"),
        sa.Column("filename", sa.String(255), nullable=False, comment="提交时文件名快照"),
        sa.Column("media_type", sa.String(128), nullable=False, comment="提交时文件类型快照"),
        sa.Column("byte_size", sa.BigInteger(), nullable=False, comment="提交时文件大小快照"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        sa.UniqueConstraint("message_id", "artifact_id", name="uq_yp_agent_message_attachment_message_artifact"),
        sa.UniqueConstraint("message_id", "position", name="uq_yp_agent_message_attachment_message_position"),
        comment="会话用户消息的上传文件引用",
    )
    op.create_index("ix_yp_agent_message_attachment_message_id", "yp_agent_message_attachment", ["message_id"])


def downgrade() -> None:
    """移除消息附件关系。"""
    op.drop_index("ix_yp_agent_message_attachment_message_id", table_name="yp_agent_message_attachment")
    op.drop_table("yp_agent_message_attachment")
