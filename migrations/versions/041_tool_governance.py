# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:12:00
# @Author  : yangyuexiong
# @File    : 041_tool_governance.py

"""增加凭据撤销水位、恢复契约和跨进程并发租约。"""

from alembic import op
import sqlalchemy as sa

from app.models.base import ShanghaiDateTime

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留历史版本摘要，历史外部写调用默认禁止自动重放。"""
    with op.batch_alter_table("yp_user_tool") as batch:
        batch.add_column(sa.Column("credentials_revoked_through", sa.Integer(), nullable=False, server_default="0", comment="已撤销凭据的最大 Tool 版本号"))
    with op.batch_alter_table("yp_user_tool") as batch:
        batch.alter_column("credentials_revoked_through", existing_type=sa.Integer(), server_default=None)
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.add_column(sa.Column("recovery_policy", sa.String(24), nullable=False, server_default="manual", comment="调用恢复策略"))
        batch.add_column(sa.Column("idempotency_key_field", sa.String(128), nullable=True, comment="远端幂等键参数名"))
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.add_column(
            sa.Column("write_contract_verified", sa.Boolean(), nullable=False, server_default=sa.false(), comment="外部写恢复契约是否经管理员验收"),
        )
        batch.add_column(sa.Column("write_contract_evidence_hash", sa.String(64), nullable=True, comment="外部写恢复验收证据引用摘要"))
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.alter_column("write_contract_verified", existing_type=sa.Boolean(), server_default=None)
    op.execute(sa.text("UPDATE yp_user_tool_version SET recovery_policy = 'safe' WHERE effect = 'read'"))
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.alter_column("recovery_policy", existing_type=sa.String(24), server_default=None)
    if op.get_bind().dialect.name == "mysql":
        for name, column_type, comment in (
            ("requirements", sa.JSON(), "用户声明的 Python 依赖清单"),
            ("environment_state", sa.String(24), "脚本依赖环境准备状态"),
            ("environment_attempts", sa.Integer(), "环境准备尝试次数"),
        ):
            op.alter_column("yp_user_tool_version", name, existing_type=column_type, existing_nullable=False, comment=comment)
    op.create_table(
        "yp_tool_execution_gate",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("revision", sa.Integer(), nullable=False, comment="闸门锁定版本"),
        comment="工具并发容量判定闸门",
    )
    op.create_table(
        "yp_tool_execution_lease",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("token", sa.String(36), nullable=False, unique=True, comment="资源租约令牌"),
        sa.Column("kind", sa.String(24), nullable=False, comment="工具调用或依赖准备"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="实际使用用户 ID"),
        sa.Column("run_id", sa.String(36), nullable=True, comment="关联运行 ID，试运行或准备时为空"),
        sa.Column("expires_at", ShanghaiDateTime(), nullable=False, comment="资源租约到期时间"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="工具调用与依赖准备资源租约",
    )
    for name in ("kind", "owner_id", "run_id", "expires_at"):
        op.create_index(f"ix_yp_tool_execution_lease_{name}", "yp_tool_execution_lease", [name])


def downgrade() -> None:
    """移除治理字段及短期租约，不删除 Tool 历史业务数据。"""
    op.drop_table("yp_tool_execution_lease")
    op.drop_table("yp_tool_execution_gate")
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.drop_column("write_contract_evidence_hash")
        batch.drop_column("write_contract_verified")
        batch.drop_column("idempotency_key_field")
        batch.drop_column("recovery_policy")
    with op.batch_alter_table("yp_user_tool") as batch:
        batch.drop_column("credentials_revoked_through")
