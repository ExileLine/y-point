# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 03:00:26
# @Author  : yangyuexiong
# @File    : 019_run_tool_events.py

"""为运行事件增加不含工具参数与结果的关联字段。

Revision ID: 019
Revises: 018
"""

import sqlalchemy as sa
from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """记录服务端工具调用 ID 和已授权工具名称。"""
    op.add_column("yp_agent_run_event", sa.Column("tool_call_id", sa.String(36), nullable=True, comment="服务端生成的工具调用关联 ID"))
    op.add_column("yp_agent_run_event", sa.Column("tool_name", sa.String(128), nullable=True, comment="已授权工具名称"))


def downgrade() -> None:
    """移除工具调用关联字段。"""
    op.drop_column("yp_agent_run_event", "tool_name")
    op.drop_column("yp_agent_run_event", "tool_call_id")
