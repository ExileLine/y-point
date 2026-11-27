# -*- coding: utf-8 -*-
# @Time    : 2026-11-27 13:49:41
# @Author  : yangyuexiong
# @File    : 069_approval_without_deadline.py

"""人工审批默认无截止时间，仅清除当前仍在等待的历史审批期限。"""

from alembic import op
import sqlalchemy as sa

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许空截止时间并保留已决定、已超时等历史审批与运行终态。"""
    with op.batch_alter_table("yp_run_approval") as batch:
        batch.alter_column(
            "expires_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            nullable=True,
            existing_comment="审批到期时间",
            comment="审批到期时间，空值表示等待用户决定",
        )
    approvals = sa.table(
        "yp_run_approval",
        sa.column("run_id", sa.String()),
        sa.column("checkpoint_id", sa.String()),
        sa.column("state", sa.String()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
    )
    runs = sa.table(
        "yp_agent_run",
        sa.column("id", sa.String()),
        sa.column("checkpoint_id", sa.String()),
        sa.column("state", sa.String()),
    )
    matching_run = sa.exists().where(
        runs.c.id == approvals.c.run_id,
        runs.c.checkpoint_id == approvals.c.checkpoint_id,
        runs.c.state == "waiting_for_approval",
    )
    op.execute(approvals.update().where(approvals.c.state == "pending", matching_run).values(expires_at=None))


def downgrade() -> None:
    """存在无限等待审批时拒绝回退，避免静默恢复超时并结束用户任务。"""
    approvals = sa.table(
        "yp_run_approval",
        sa.column("state", sa.String()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
        sa.column("decided_at", sa.DateTime(timezone=True)),
        sa.column("create_time", sa.DateTime(timezone=True)),
    )
    pending = op.get_bind().scalar(sa.select(sa.func.count()).select_from(approvals).where(approvals.c.state == "pending", approvals.c.expires_at.is_(None)))
    if pending:
        raise RuntimeError("仍有无截止时间的待审批请求，请先决定或取消相关任务后再回退")
    op.execute(
        approvals.update().where(approvals.c.expires_at.is_(None)).values(expires_at=sa.func.coalesce(approvals.c.decided_at, approvals.c.create_time)),
    )
    with op.batch_alter_table("yp_run_approval") as batch:
        batch.alter_column(
            "expires_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=True,
            nullable=False,
            existing_comment="审批到期时间，空值表示等待用户决定",
            comment="审批到期时间",
        )
