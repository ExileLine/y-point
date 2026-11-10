# -*- coding: utf-8 -*-
# @Time    : 2026-11-09 16:51:23
# @Author  : yangyuexiong
# @File    : 047_configurable_runtime_defaults.py

"""移除旧的数据库超时默认值，由应用按部署配置明确写入。

Revision ID: 047
Revises: 046
"""

from alembic import op
import sqlalchemy as sa

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留现有 Agent 数值和 Run 快照，仅移除固定的数据库默认值。"""
    for table in ("yp_agent_config", "yp_agent_run"):
        with op.batch_alter_table(table) as batch:
            for column in ("model_timeout_seconds", "tool_timeout_seconds"):
                batch.alter_column(column, existing_type=sa.Integer(), existing_nullable=False, server_default=None)


def downgrade() -> None:
    """恢复历史版本的默认值，已有参数和快照保持原值。"""
    for table in ("yp_agent_config", "yp_agent_run"):
        with op.batch_alter_table(table) as batch:
            for column in ("model_timeout_seconds", "tool_timeout_seconds"):
                batch.alter_column(column, existing_type=sa.Integer(), existing_nullable=False, server_default="120")
