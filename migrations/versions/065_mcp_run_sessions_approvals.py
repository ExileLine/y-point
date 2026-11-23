# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : 065_mcp_run_sessions_approvals.py

"""保存逐项审批决策和 MCP 会话 Worker 归属，不改变历史拒绝语义。"""

from alembic import op
import sqlalchemy as sa

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史审批保持空决策，历史运行不假定已拥有远端会话。"""
    op.create_table(
        "yp_run_mcp_resource",
        sa.Column("id", sa.String(36), primary_key=True, comment="远端会话回收凭单 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="所属业务 Run"),
        sa.Column("encrypted_payload", sa.Text(), nullable=False, comment="以部署密钥加密的回收地址、凭据、会话 ID 和协议版本"),
        sa.Column("retired", sa.Boolean(), nullable=False, comment="本地会话已退出但远端尚未确认删除"),
        sa.Column("expires_at", sa.DateTime(), nullable=False, comment="会话最长存活截止时间"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False, comment="下次尽力回收时间"),
        sa.Column("create_time", sa.DateTime(), nullable=False),
        sa.Column("update_time", sa.DateTime(), nullable=False),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_yp_run_mcp_resource_run_id", "yp_run_mcp_resource", ["run_id"])
    with op.batch_alter_table("yp_run_approval") as batch:
        batch.alter_column(
            "state", existing_type=sa.String(16), existing_nullable=False, comment="审批状态：pending、approved、resolved、rejected、expired 或 cancelled"
        )
        batch.add_column(sa.Column("decision", sa.String(24), nullable=True, comment="显式审批动作；空值兼容旧请求"))
        batch.add_column(sa.Column("decisions", sa.JSON(), nullable=True, comment="按动作顺序保存批准或拒绝，恢复时逐项绑定"))
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.add_column(sa.Column("mcp_worker_id", sa.String(36), nullable=True, comment="持有本轮 MCP 会话的 Worker 启动身份"))
        batch.add_column(sa.Column("mcp_owner_expires_at", sa.DateTime(), nullable=True, comment="MCP 资源拥有者心跳到期；过期不转移远端状态"))
        batch.create_index("ix_yp_agent_run_mcp_worker_id", ["mcp_worker_id"])


def downgrade() -> None:
    """含新决策或会话归属时拒绝回退，避免旧 Worker 错把拒绝当批准。"""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM yp_run_mcp_resource")).scalar_one():
        raise RuntimeError("仍有 MCP 远端会话待回收，请先完成清理后回退")
    if bind.execute(sa.text("SELECT COUNT(*) FROM yp_run_approval WHERE decision IS NOT NULL")).scalar_one():
        raise RuntimeError("仍有新版逐项审批审计，请先备份并迁出后回退")
    if bind.execute(sa.text("SELECT COUNT(*) FROM yp_agent_run WHERE mcp_worker_id IS NOT NULL")).scalar_one():
        raise RuntimeError("仍有 MCP 会话归属记录，请先停止任务、清理资源并备份后回退")
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.drop_index("ix_yp_agent_run_mcp_worker_id")
        batch.drop_column("mcp_owner_expires_at")
        batch.drop_column("mcp_worker_id")
    with op.batch_alter_table("yp_run_approval") as batch:
        batch.alter_column("state", existing_type=sa.String(16), existing_nullable=False, comment="审批状态：pending、approved、rejected、expired 或 cancelled")
        batch.drop_column("decisions")
        batch.drop_column("decision")

    op.drop_table("yp_run_mcp_resource")
