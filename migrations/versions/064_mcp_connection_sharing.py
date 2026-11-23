# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 18:00:00
# @Author  : yangyuexiong
# @File    : 064_mcp_connection_sharing.py

"""添加指定用户连接授权和 Agent 明确工具子集，保留历史直接绑定。"""

from alembic import op
import sqlalchemy as sa

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None


def audit_columns() -> list:
    """保持通用模型审计列与既有表一致。"""
    return [
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """新增关系表；旧 AgentTool 与 RunTool 不展开或改写。"""
    op.create_table(
        "yp_mcp_connection_share",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("connection_id", sa.Integer(), nullable=False, comment="共享连接 ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="获授权用户 ID"),
        sa.Column("version", sa.Integer(), nullable=False, comment="共享授权版本，用于并发修改检查"),
        sa.Column("tool_ids", sa.JSON(), nullable=False, comment="明确授权的已发布工具 ID，不包含未来新工具"),
        sa.Column("allow_credentials", sa.Boolean(), nullable=False, comment="是否允许在授权工具中借用凭据，不返回密钥"),
        *audit_columns(),
        sa.UniqueConstraint("connection_id", "user_id", name="uq_yp_mcp_connection_share_user"),
        comment="指定用户的连接摘要、工具子集及凭据借用授权",
    )
    for column in ("connection_id", "user_id"):
        op.create_index(f"ix_yp_mcp_connection_share_{column}", "yp_mcp_connection_share", [column])
    op.create_table(
        "yp_agent_mcp_tool",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="Agent ID"),
        sa.Column("connection_id", sa.Integer(), nullable=False, comment="关联连接 ID"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="明确选择的已发布工具 ID"),
        sa.Column("tool_version", sa.Integer(), nullable=False, comment="固定工具版本，升级须显式确认"),
        *audit_columns(),
        sa.UniqueConstraint("agent_id", "connection_id", "tool_id", name="uq_yp_agent_mcp_tool_binding"),
        comment="Agent 按连接明确选择的工具及固定版本",
    )
    for column in ("agent_id", "connection_id"):
        op.create_index(f"ix_yp_agent_mcp_tool_{column}", "yp_agent_mcp_tool", [column])
    with op.batch_alter_table("yp_run_tool") as batch:
        batch.add_column(sa.Column("mcp_connection_id", sa.Integer(), nullable=True, comment="按连接展开的授权来源，空值兼容直接绑定"))


def downgrade() -> None:
    """旧代码不能表达按连接授权；有新关系或历史来源快照时拒绝有损回退。"""
    bind = op.get_bind()
    for table in ("yp_mcp_connection_share", "yp_agent_mcp_tool"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError("仍有 MCP 连接共享或 Agent 连接选择，请先备份、撤销共享并转换为直接绑定后回退")
    if bind.execute(sa.text("SELECT COUNT(*) FROM yp_run_tool WHERE mcp_connection_id IS NOT NULL")).scalar_one():
        raise RuntimeError("仍有按连接授权的历史 Run 快照，请先备份并迁出后回退")
    with op.batch_alter_table("yp_run_tool") as batch:
        batch.drop_column("mcp_connection_id")
    for table in ("yp_agent_mcp_tool", "yp_mcp_connection_share"):
        op.drop_table(table)
