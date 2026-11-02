# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : 039_user_tools.py

"""新增用户 Tool 版本、Agent/Run 关联及调用记录。

Revision ID: 039
Revises: 038
"""

import sqlalchemy as sa
from alembic import op

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def _audit_columns():
    """生成业务模型通用审计字段。"""
    return [
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """创建用户 Tool 的五张业务表和必要索引。"""
    op.create_table(
        "yp_user_tool",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="创建用户 ID"),
        sa.Column("name", sa.String(64), nullable=False, comment="创建者范围内的 Tool 名称"),
        sa.Column("description", sa.String(1024), nullable=False, comment="模型可见的 Tool 用途说明"),
        sa.Column("kind", sa.String(16), nullable=False, comment="脚本或 MCP 来源类型"),
        sa.Column("visibility", sa.String(16), nullable=False, comment="私有或公开"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否启用"),
        sa.Column("current_version", sa.Integer(), nullable=False, comment="当前发布版本号"),
        *_audit_columns(),
        sa.UniqueConstraint("owner_id", "name", name="uq_yp_user_tool_owner_name"),
        comment="用户创建的脚本或 MCP Tool",
    )
    op.create_index("ix_yp_user_tool_owner_id", "yp_user_tool", ["owner_id"])
    op.create_table(
        "yp_user_tool_version",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="Tool ID"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("content_hash", sa.String(64), nullable=False, comment="执行定义 SHA256"),
        sa.Column("input_schema", sa.JSON(), nullable=False, comment="工具输入 JSON Schema"),
        sa.Column("effect", sa.String(16), nullable=False, comment="只读或外部写入"),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, comment="调用前是否要求审批"),
        sa.Column("verified", sa.Boolean(), nullable=False, comment="当前版本是否通过发布验证"),
        sa.Column("script_language", sa.String(16), comment="脚本语言"),
        sa.Column("source_code", sa.Text(), comment="脚本源代码"),
        sa.Column("mcp_url", sa.String(512), comment="MCP Streamable HTTP 地址"),
        sa.Column("mcp_tool_name", sa.String(128), comment="远端 MCP 工具名"),
        sa.Column("encrypted_credentials", sa.Text(), comment="加密的 MCP 连接凭据"),
        sa.Column("share_credentials", sa.Boolean(), nullable=False, comment="公开时是否授权共享连接凭据"),
        *_audit_columns(),
        sa.UniqueConstraint("tool_id", "version", name="uq_yp_user_tool_version_number"),
        comment="用户 Tool 不可变版本",
    )
    op.create_index("ix_yp_user_tool_version_tool_id", "yp_user_tool_version", ["tool_id"])
    op.create_table(
        "yp_agent_tool",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="智能体 ID"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="Tool ID"),
        sa.Column("tool_version", sa.Integer(), nullable=False, comment="固定 Tool 版本号"),
        *_audit_columns(),
        sa.UniqueConstraint("agent_id", "tool_id", name="uq_yp_agent_tool_pair"),
        comment="智能体直接关联的 Tool 版本",
    )
    op.create_index("ix_yp_agent_tool_agent_id", "yp_agent_tool", ["agent_id"])
    op.create_table(
        "yp_run_tool",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="运行 ID"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="Tool ID"),
        sa.Column("tool_version", sa.Integer(), nullable=False, comment="运行固定版本号"),
        sa.Column("content_hash", sa.String(64), nullable=False, comment="运行固定执行定义摘要"),
        *_audit_columns(),
        sa.UniqueConstraint("run_id", "tool_id", name="uq_yp_run_tool_pair"),
        comment="运行可调用的 Tool 版本快照",
    )
    op.create_index("ix_yp_run_tool_run_id", "yp_run_tool", ["run_id"])
    op.create_table(
        "yp_tool_invocation",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="运行 ID"),
        sa.Column("call_id", sa.String(36), nullable=False, comment="模型工具调用 ID"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="Tool ID"),
        sa.Column("tool_version", sa.Integer(), nullable=False, comment="调用 Tool 版本号"),
        sa.Column("state", sa.String(24), nullable=False, comment="调用状态"),
        sa.Column("arguments_hash", sa.String(64), nullable=False, comment="输入参数 SHA256"),
        sa.Column("failure_code", sa.String(32), comment="脱敏失败类别"),
        *_audit_columns(),
        sa.UniqueConstraint("run_id", "call_id", name="uq_yp_tool_invocation_call"),
        comment="用户 Tool 调用审计记录",
    )
    op.create_index("ix_yp_tool_invocation_run_id", "yp_tool_invocation", ["run_id"])


def downgrade() -> None:
    """按依赖顺序移除用户 Tool 表。"""
    for table, index in (
        ("yp_tool_invocation", "ix_yp_tool_invocation_run_id"),
        ("yp_run_tool", "ix_yp_run_tool_run_id"),
        ("yp_agent_tool", "ix_yp_agent_tool_agent_id"),
        ("yp_user_tool_version", "ix_yp_user_tool_version_tool_id"),
        ("yp_user_tool", "ix_yp_user_tool_owner_id"),
    ):
        op.drop_index(index, table_name=table)
        op.drop_table(table)
