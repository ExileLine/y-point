# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 09:11:53
# @Author  : yangyuexiong
# @File    : 001_initial.py

"""Create initial tables for the existing models.

Revision ID: 001
Revises:
"""

from alembic import op
import sqlalchemy as sa


revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建用户表和定时任务表。"""
    op.create_table(
        "yp_admin",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("username", sa.String(255), comment="用户名"),
        sa.Column("password", sa.String(255), nullable=False, comment="密码"),
        sa.Column("nickname", sa.String(128), comment="昵称"),
        sa.Column("phone", sa.String(64), comment="手机号"),
        sa.Column("mail", sa.String(256), comment="邮箱"),
        sa.Column("code", sa.String(64), comment="用户编号"),
        sa.Column("seat", sa.String(64), comment="座位编号"),
        sa.Column("department", sa.String(64), comment="部门"),
        sa.Column("position", sa.String(64), comment="职位"),
        sa.Column("superior", sa.String(64), comment="上级"),
        sa.Column("login_type", sa.String(64), comment="登录类型:single;many"),
        sa.Column("is_tourist", sa.Integer(), nullable=False, comment="0-游客账户;1-非游客账户"),
        sa.Column("creator", sa.String(32), comment="创建人"),
        sa.Column("creator_id", sa.BigInteger(), comment="创建人id"),
        sa.Column("modifier", sa.String(32), comment="更新人"),
        sa.Column("modifier_id", sa.BigInteger(), comment="更新人id"),
        sa.Column("remark", sa.String(255), comment="备注"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="用户表",
    )
    op.create_table(
        "yp_aps_tasks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("task_id", sa.String(64), nullable=False, comment="任务id"),
        sa.Column("trigger_type", sa.String(16), nullable=False, comment="触发器类型:date;interval;cron"),
        sa.Column("trigger_param", sa.JSON(), nullable=False, comment="触发器参数"),
        sa.Column("task_function_name", sa.String(255), comment="任务函数名称"),
        sa.Column("task_function_args", sa.JSON(), nullable=False, comment="任务参数:args"),
        sa.Column("task_function_kwargs", sa.JSON(), nullable=False, comment="任务参数:kwargs"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="定时任务表",
    )


def downgrade() -> None:
    """删除首个迁移创建的业务表。"""
    op.drop_table("yp_aps_tasks")
    op.drop_table("yp_admin")
