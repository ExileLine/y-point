# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:58:12
# @Author  : yangyuexiong
# @File    : 007_agent_runs.py

"""创建运行任务和可回放事件表。

Revision ID: 007
Revises: 006
"""

import sqlalchemy as sa
from alembic import op

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def audit_columns() -> list[sa.Column]:
    """返回与 CustomBaseModel 一致的公共审计字段。"""
    return [
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """建立不使用外键的运行和事件存储。"""
    op.create_table(
        "yp_agent_run",
        sa.Column("id", sa.String(36), primary_key=True, comment="运行 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="运行所属用户 ID"),
        sa.Column("agent_id", sa.Integer(), comment="关联智能体 ID"),
        sa.Column("model_id", sa.Integer(), comment="运行时模型 ID"),
        sa.Column("thread_id", sa.String(36), nullable=False, comment="检查点线程 ID"),
        sa.Column("request_key", sa.String(128), comment="用户提交幂等键"),
        sa.Column("state", sa.String(32), nullable=False, comment="运行状态"),
        sa.Column("version", sa.Integer(), nullable=False, comment="并发更新版本号"),
        sa.Column("event_seq", sa.Integer(), nullable=False, comment="最新事件序号"),
        sa.Column("lease_token", sa.String(36), comment="执行器租约令牌"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), comment="执行器租约到期时间"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), comment="执行器最近心跳时间"),
        sa.Column("checkpoint_id", sa.String(128), comment="最近持久化检查点 ID"),
        sa.Column("failure_code", sa.String(32), comment="脱敏运行故障类别"),
        *audit_columns(),
        sa.UniqueConstraint("thread_id", name="uq_yp_agent_run_thread_id"),
        sa.UniqueConstraint("owner_id", "request_key", name="uq_yp_agent_run_owner_request_key"),
        comment="智能体运行任务表",
    )
    op.create_index("ix_yp_agent_run_owner_id", "yp_agent_run", ["owner_id"])
    op.create_table(
        "yp_agent_run_event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="所属运行 ID"),
        sa.Column("sequence", sa.Integer(), nullable=False, comment="运行内单调事件序号"),
        sa.Column("protocol_version", sa.Integer(), nullable=False, comment="事件协议版本"),
        sa.Column("event_type", sa.String(32), nullable=False, comment="事件类型"),
        sa.Column("state", sa.String(32), nullable=False, comment="事件发生后的运行状态"),
        sa.Column("failure_code", sa.String(32), comment="脱敏运行故障类别"),
        *audit_columns(),
        sa.UniqueConstraint("run_id", "sequence", name="uq_yp_agent_run_event_run_sequence"),
        comment="智能体运行事件表",
    )
    op.create_index("ix_yp_agent_run_event_run_id", "yp_agent_run_event", ["run_id"])


def downgrade() -> None:
    """删除运行事件和运行任务表。"""
    op.drop_index("ix_yp_agent_run_event_run_id", table_name="yp_agent_run_event")
    op.drop_table("yp_agent_run_event")
    op.drop_index("ix_yp_agent_run_owner_id", table_name="yp_agent_run")
    op.drop_table("yp_agent_run")
