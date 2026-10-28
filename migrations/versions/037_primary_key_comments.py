# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 16:45:20
# @Author  : yangyuexiong
# @File    : 037_primary_key_comments.py

"""同步已有业务表自增主键的中文列注释。

Revision ID: 037
Revises: 036
"""

import sqlalchemy as sa
from alembic import op

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None

INTEGER_KEY_TABLES = (
    "yp_admin",
    "yp_agent_config",
    "yp_agent_message_attachment",
    "yp_agent_run_event",
    "yp_agent_run_message",
    "yp_agent_share",
    "yp_aps_tasks",
    "yp_artifact_source",
    "yp_audit_event",
    "yp_model_call_record",
    "yp_model_catalog",
    "yp_provider_connection",
    "yp_run_artifact_read",
)


def _set_comments(comment: str) -> None:
    """按方言更新已存在的自增主键注释，SQLite 无列注释能力时跳过。"""
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        return
    for table in INTEGER_KEY_TABLES:
        kwargs = (
            {
                "existing_autoincrement": True,
            }
            if dialect == "mysql"
            else {}
        )
        op.alter_column(table, "id", existing_type=sa.Integer(), existing_nullable=False, comment=comment, **kwargs)


def upgrade() -> None:
    """为既有 PostgreSQL/MySQL 业务表的自增主键写入中文注释。"""
    _set_comments("主键 ID")


def downgrade() -> None:
    """回退既有 PostgreSQL/MySQL 业务表的自增主键注释。"""
    _set_comments("ID")
