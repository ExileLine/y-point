# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:17:33
# @Author  : yangyuexiong
# @File    : 058_mcp_connections.py

"""新增可复用 MCP 连接与 Tool 的固定连接快照。"""

from alembic import op
import sqlalchemy as sa

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加连接实体和固定版本字段，不改变现有 HTTP 工具的执行定义。"""
    op.create_table(
        "yp_mcp_connection",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="连接所有者 ID"),
        sa.Column("name", sa.String(64), nullable=False, comment="所有者范围内的连接名称"),
        sa.Column("version", sa.Integer(), nullable=False, comment="当前连接配置版本"),
        sa.Column("config", sa.JSON(), nullable=False, comment="不含凭据的传输与受控执行配置"),
        sa.Column("encrypted_credentials", sa.Text(), nullable=True, comment="加密的 Bearer 或环境变量凭据"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否允许发现及执行"),
        sa.Column("share_credentials", sa.Boolean(), nullable=False, comment="是否允许公开工具借用连接凭据"),
        sa.Column("credentials_revoked_through", sa.Integer(), nullable=False, comment="已撤销凭据的最大连接版本"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("owner_id", "name", name="uq_yp_mcp_connection_owner_name"),
        comment="可由多个工具复用的 MCP 连接",
    )
    op.create_index("ix_yp_mcp_connection_owner_id", "yp_mcp_connection", ["owner_id"])
    op.add_column("yp_user_tool_version", sa.Column("mcp_connection_id", sa.Integer(), nullable=True, comment="所属 MCP 连接 ID"))
    op.add_column("yp_user_tool_version", sa.Column("mcp_connection_version", sa.Integer(), nullable=True, comment="固定的 MCP 连接版本"))
    op.add_column("yp_user_tool_version", sa.Column("mcp_config", sa.JSON(), nullable=True, comment="固定且不含凭据的 MCP 连接配置"))
    op.create_index("ix_yp_user_tool_version_mcp_connection_id", "yp_user_tool_version", ["mcp_connection_id"])


def downgrade() -> None:
    """拒绝把已使用新传输的版本静默降成旧定义，空使用环境可回退。"""
    count = op.get_bind().execute(sa.text("SELECT count(*) FROM yp_user_tool_version WHERE mcp_connection_id IS NOT NULL")).scalar()
    if count:
        raise RuntimeError("已有工具引用 MCP 连接，请先备份并清理这些工具版本后再降级")
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.drop_index("ix_yp_user_tool_version_mcp_connection_id")
        batch.drop_column("mcp_config")
        batch.drop_column("mcp_connection_version")
        batch.drop_column("mcp_connection_id")
    op.drop_index("ix_yp_mcp_connection_owner_id", table_name="yp_mcp_connection")
    op.drop_table("yp_mcp_connection")
