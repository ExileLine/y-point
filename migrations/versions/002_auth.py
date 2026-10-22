# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 16:46:46
# @Author  : yangyuexiong
# @File    : 002_auth.py

"""新增认证、授权与审计相关数据库结构。

Revision ID: 002
Revises: 001
"""

from alembic import op
import sqlalchemy as sa


revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def _audit_columns():
    """返回与 CustomBaseModel 一致的审计字段定义。"""
    return [
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """扩展用户表并创建无数据库外键的认证和审计表。"""
    with op.batch_alter_table("yp_admin") as batch:
        batch.add_column(
            sa.Column("role", sa.String(16), nullable=False, server_default="user", comment="用户角色：admin 管理员，user 普通用户")
        )
        batch.add_column(sa.Column("token_version", sa.Integer(), nullable=False, server_default="0", comment="令牌版本"))
        batch.add_column(sa.Column("last_login_at", sa.DateTime(timezone=True), comment="最近登录时间"))
        batch.create_index("uq_yp_admin_username", ["username"], unique=True)
        batch.create_index("uq_yp_admin_mail", ["mail"], unique=True)

    op.create_table(
        "yp_auth_session",
        sa.Column("id", sa.String(36), primary_key=True, comment="会话 ID"),
        sa.Column("admin_id", sa.Integer(), nullable=False, comment="所属用户 ID"),
        sa.Column("refresh_hash", sa.String(64), nullable=False, comment="刷新令牌哈希"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False, comment="会话到期时间"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), comment="会话撤销时间"),
        *_audit_columns(),
        comment="用户登录会话表",
    )
    op.create_index("ix_yp_auth_session_admin_id", "yp_auth_session", ["admin_id"])
    op.create_table(
        "yp_auth_attempt",
        sa.Column("id", sa.String(64), primary_key=True, comment="账号或来源地址哈希"),
        sa.Column("failures", sa.Integer(), nullable=False, comment="窗口内失败次数"),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False, comment="计数窗口开始时间"),
        sa.Column("locked_until", sa.DateTime(timezone=True), comment="限制登录截止时间"),
        *_audit_columns(),
        comment="登录失败次数表",
    )
    op.create_table(
        "yp_audit_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="ID"),
        sa.Column("actor_id", sa.Integer(), comment="操作用户 ID"),
        sa.Column("target_id", sa.Integer(), comment="目标用户 ID"),
        sa.Column("action", sa.String(64), nullable=False, comment="操作类型"),
        sa.Column("subject_hash", sa.String(64), comment="登录标识哈希"),
        *_audit_columns(),
        comment="用户操作审计表",
    )
    op.create_index("ix_yp_audit_event_actor_id", "yp_audit_event", ["actor_id"])


def downgrade() -> None:
    """删除认证和审计结构并还原用户表。"""
    op.drop_index("ix_yp_audit_event_actor_id", table_name="yp_audit_event")
    op.drop_table("yp_audit_event")
    op.drop_table("yp_auth_attempt")
    op.drop_index("ix_yp_auth_session_admin_id", table_name="yp_auth_session")
    op.drop_table("yp_auth_session")
    with op.batch_alter_table("yp_admin") as batch:
        batch.drop_index("uq_yp_admin_mail")
        batch.drop_index("uq_yp_admin_username")
        batch.drop_column("last_login_at")
        batch.drop_column("token_version")
        batch.drop_column("role")
