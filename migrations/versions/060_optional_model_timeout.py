# -*- coding: utf-8 -*-

"""允许 Agent 和 Run 清空单次模型倒计时。

Revision ID: 060
Revises: 059
"""

from alembic import op
import sqlalchemy as sa

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """放宽可空性，保留已有 Agent 参数与 Run 快照。"""
    for name in ("yp_agent_config", "yp_agent_run"):
        with op.batch_alter_table(name) as batch:
            batch.alter_column(
                "model_timeout_seconds",
                existing_type=sa.Integer(),
                existing_nullable=False,
                nullable=True,
                existing_comment="单次模型生成超时秒数",
                comment="单次模型生成超时秒数，空值不设置平台倒计时",
            )


def downgrade() -> None:
    """仅为空值补充旧默认时限，再恢复非空约束。"""
    for name in ("yp_agent_config", "yp_agent_run"):
        table = sa.table(name, sa.column("model_timeout_seconds", sa.Integer()))
        op.execute(table.update().where(table.c.model_timeout_seconds.is_(None)).values(model_timeout_seconds=120))
        with op.batch_alter_table(name) as batch:
            batch.alter_column(
                "model_timeout_seconds",
                existing_type=sa.Integer(),
                existing_nullable=True,
                nullable=False,
                existing_comment="单次模型生成超时秒数，空值不设置平台倒计时",
                comment="单次模型生成超时秒数",
            )

