# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:29:06
# @Author  : yangyuexiong
# @File    : 024_text_artifact_tool.py

"""为运行任务记录文本文件工具的显式授权开关。

Revision ID: 024
Revises: 023
"""

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史运行保持不注册业务工具。"""
    op.add_column(
        "yp_agent_run",
        sa.Column(
            "enable_text_artifact_tool", sa.Boolean(), nullable=False, server_default="0", comment="是否启用需审批的文本文件读取工具"
        ),
    )


def downgrade() -> None:
    """移除文本文件工具授权开关。"""
    op.drop_column("yp_agent_run", "enable_text_artifact_tool")
