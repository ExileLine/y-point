# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 10:00:00
# @Author  : yangyuexiong
# @File    : 062_mcp_execution_policy.py

"""拆分 MCP 审批策略、读写证据和恢复声明。

Revision ID: 062
Revises: 061
"""

from alembic import op
import sqlalchemy as sa

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史强审批映射为 always，其他记录继承保守默认，不重写历史执行摘要。"""
    with op.batch_alter_table("yp_mcp_connection") as batch:
        batch.add_column(sa.Column("approval_mode", sa.String(16), nullable=False, server_default="writes", comment="连接默认审批方式"))
    with op.batch_alter_table("yp_mcp_connection") as batch:
        batch.alter_column("approval_mode", server_default=None)
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.add_column(sa.Column("approval_mode", sa.String(16), nullable=True, comment="工具审批覆盖，空值兼容历史布尔设置"))
        batch.add_column(sa.Column("effect_source", sa.String(24), nullable=True, comment="MCP 读写判断的证据来源"))
        batch.add_column(sa.Column("mcp_annotations", sa.JSON(), nullable=True, comment="发布时远端显式提供的 MCP 工具提示"))
        batch.add_column(sa.Column("read_confirmation", sa.JSON(), nullable=True, comment="所有者只读确认及绑定执行定义摘要"))
    table = sa.table("yp_user_tool_version", sa.column("approval_mode", sa.String()), sa.column("requires_approval", sa.Boolean()))
    op.execute(table.update().values(approval_mode=sa.case((table.c.requires_approval.is_(True), "always"), else_="inherit")))
    with op.batch_alter_table("yp_run_tool") as batch:
        batch.add_column(sa.Column("approval_policy", sa.JSON(), nullable=True, comment="提交时有效审批方式及来源快照"))
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.add_column(
            sa.Column("require_mcp_approval", sa.Boolean(), nullable=False, server_default=sa.false(), comment="本轮使用者要求所有 MCP 调用均审批")
        )
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.alter_column("require_mcp_approval", server_default=None)


def downgrade() -> None:
    """要求新策略运行先结束；以更严格的旧布尔值保存现有配置，再移除新增元数据。"""
    active = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) FROM yp_agent_run r WHERE r.state IN ('queued', 'running', 'waiting_for_approval', 'cancel_requested') "
                "AND (r.require_mcp_approval = true OR EXISTS "
                "(SELECT 1 FROM yp_run_tool t WHERE t.run_id = r.id AND t.approval_policy IS NOT NULL))"
            )
        )
        .scalar_one()
    )
    if active:
        raise RuntimeError("仍有使用新 MCP 审批策略的活动运行，请先完成或停止后再回退")
    # 保守回填，旧 Worker 仍保留其外部写幂等准入限制；不修改历史 RunTool 的审批事实。
    op.execute(
        sa.text(
            "UPDATE yp_user_tool_version SET requires_approval = true WHERE approval_mode = 'always' OR effect_source = 'owner_read' OR "
            "(approval_mode = 'inherit' AND mcp_connection_id IN "
            "(SELECT id FROM yp_mcp_connection WHERE approval_mode = 'always'))"
        )
    )
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.drop_column("require_mcp_approval")
    with op.batch_alter_table("yp_run_tool") as batch:
        batch.drop_column("approval_policy")
    with op.batch_alter_table("yp_user_tool_version") as batch:
        for name in ("read_confirmation", "mcp_annotations", "effect_source", "approval_mode"):
            batch.drop_column(name)
    with op.batch_alter_table("yp_mcp_connection") as batch:
        batch.drop_column("approval_mode")
