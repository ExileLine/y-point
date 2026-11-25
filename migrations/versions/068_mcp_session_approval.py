# -*- coding: utf-8 -*-
# @Time    : 2026-11-25 08:53:44
# @Author  : yangyuexiong
# @File    : 068_mcp_session_approval.py

"""持久化会话放行策略与审批决定范围，不改动既有审批结论。"""

from alembic import op
import sqlalchemy as sa

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为空会话保留逐次询问，历史审批均视为仅批准本次。"""
    op.add_column("yp_agent_session", sa.Column("approval_policy", sa.JSON(), nullable=True, comment="会话批准的 MCP 工具身份与版本集合；空值表示逐次询问"))
    op.add_column(
        "yp_agent_session",
        sa.Column("approval_policy_version", sa.Integer(), nullable=False, server_default="1", comment="会话审批放行策略并发版本"),
    )
    op.add_column("yp_run_approval", sa.Column("scope", sa.String(16), nullable=False, server_default="once", comment="本次决定附带的会话放行范围"))


def downgrade() -> None:
    """移除会话放行元数据，既有会话、运行与审批记录保持不变。"""
    op.drop_column("yp_run_approval", "scope")
    op.drop_column("yp_agent_session", "approval_policy_version")
    op.drop_column("yp_agent_session", "approval_policy")
