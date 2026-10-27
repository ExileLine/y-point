# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:23:43
# @Author  : yangyuexiong
# @File    : 030_agent_sessions.py

"""保存私有会话及每次运行的 Agent 配置快照。

Revision ID: 030
Revises: 029
"""

import sqlalchemy as sa
from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加无外键会话表及 Run 的配置快照字段。"""
    op.create_table(
        "yp_agent_session",
        sa.Column("id", sa.String(36), primary_key=True, comment="会话 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="会话所属用户 ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="会话所属智能体 ID"),
        sa.Column("thread_id", sa.String(36), nullable=False, comment="专属检查点线程 ID"),
        sa.Column("title", sa.String(128), nullable=False, comment="会话标题"),
        sa.Column("archived", sa.Boolean(), nullable=False, comment="是否已归档"),
        sa.Column("version", sa.Integer(), nullable=False, comment="会话并发更新版本号"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        sa.UniqueConstraint("thread_id", name="uq_yp_agent_session_thread_id"),
        comment="智能体私有对话会话表",
    )
    op.create_index("ix_yp_agent_session_owner_id", "yp_agent_session", ["owner_id"])
    op.create_index("ix_yp_agent_session_agent_id", "yp_agent_session", ["agent_id"])
    for column in (
        sa.Column("session_id", sa.String(36), comment="关联会话 ID"),
        sa.Column("agent_config_version", sa.Integer(), comment="提交时智能体配置版本"),
        sa.Column("agent_system_prompt", sa.Text(), comment="提交时系统指令快照"),
        sa.Column("agent_temperature", sa.Numeric(3, 2), comment="提交时生成温度快照"),
        sa.Column("agent_max_output_tokens", sa.Integer(), comment="提交时最大输出 Token 数快照"),
    ):
        op.add_column("yp_agent_run", column)
    op.create_index("ix_yp_agent_run_session_id", "yp_agent_run", ["session_id"])


def downgrade() -> None:
    """移除会话索引、快照字段和会话表。"""
    op.drop_index("ix_yp_agent_run_session_id", table_name="yp_agent_run")
    for name in (
        "agent_max_output_tokens", "agent_temperature", "agent_system_prompt", "agent_config_version", "session_id",
    ):
        op.drop_column("yp_agent_run", name)
    op.drop_index("ix_yp_agent_session_agent_id", table_name="yp_agent_session")
    op.drop_index("ix_yp_agent_session_owner_id", table_name="yp_agent_session")
    op.drop_table("yp_agent_session")
