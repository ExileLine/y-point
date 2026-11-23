# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 15:00:00
# @Author  : yangyuexiong
# @File    : 063_mcp_auth_and_sync.py

"""补充 MCP 数据范围撤销和批量发布幂等回执。"""

import json

from alembic import op
import sqlalchemy as sa

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧连接默认无范围撤销；鉴权沿用 JSON 快照，历史执行摘要保持不变。"""
    with op.batch_alter_table("yp_mcp_connection") as batch:
        batch.add_column(sa.Column("config_revoked_through", sa.Integer(), nullable=False, server_default="0", comment="已撤销数据范围配置的最大连接版本"))
    with op.batch_alter_table("yp_mcp_connection") as batch:
        batch.alter_column("config_revoked_through", server_default=None)
    op.create_table(
        "yp_mcp_operation",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="实际确认操作的用户 ID"),
        sa.Column("kind", sa.String(24), nullable=False, comment="连接同步或 Agent 工具升级"),
        sa.Column("target_id", sa.Integer(), nullable=False, comment="连接或 Agent ID"),
        sa.Column("request_id", sa.String(64), nullable=False, comment="客户端稳定重试 ID"),
        sa.Column("input_hash", sa.String(64), nullable=False, comment="确认请求摘要"),
        sa.Column("result", sa.JSON(), nullable=False, comment="逐项操作结果，不含凭据与内部连接配置"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("owner_id", "kind", "target_id", "request_id", name="uq_yp_mcp_operation_request"),
        comment="MCP 同步和 Agent 版本升级的幂等回执",
    )
    op.create_index("ix_yp_mcp_operation_owner_id", "yp_mcp_operation", ["owner_id"])


def downgrade() -> None:
    """旧代码无法保留新鉴权或范围撤销语义时拒绝回退，避免静默丢失租户约束。"""
    bind = op.get_bind()
    revoked = bind.execute(sa.text("SELECT COUNT(*) FROM yp_mcp_connection WHERE config_revoked_through > 0")).scalar_one()
    configs = bind.execute(sa.text("SELECT config FROM yp_mcp_connection UNION ALL SELECT mcp_config FROM yp_user_tool_version WHERE mcp_config IS NOT NULL"))
    enhanced = False
    for row in configs:
        config = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if config.get("auth_type") == "api_key" or config.get("headers") or config.get("sensitive_header_names"):
            enhanced = True
    if revoked or enhanced:
        raise RuntimeError("仍有新 MCP 鉴权、Headers 或数据范围撤销记录，请先备份并迁出相关连接与工具版本后再回退")
    op.drop_index("ix_yp_mcp_operation_owner_id", table_name="yp_mcp_operation")
    op.drop_table("yp_mcp_operation")
    with op.batch_alter_table("yp_mcp_connection") as batch:
        batch.drop_column("config_revoked_through")
