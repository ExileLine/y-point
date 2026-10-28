# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 09:18:58
# @Author  : yangyuexiong
# @File    : 028_active_checkpoint_thread.py

"""限制每个检查点线程同时只能有一个未结束运行。

Revision ID: 028
Revises: 027
"""

import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """检查旧数据冲突，为活动运行回填并约束线程占用标记。"""
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            "SELECT thread_id FROM yp_agent_run WHERE state IN "
            "('queued', 'running', 'waiting_for_approval', 'cancel_requested') "
            "GROUP BY thread_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate is not None:
        raise RuntimeError("存在同一检查点线程的多个未结束运行，请先处理冲突再升级")
    op.add_column("yp_agent_run", sa.Column("active_thread_id", sa.String(36), nullable=True, comment="未结束运行占用的检查点线程 ID"))
    connection.execute(
        sa.text(
            "UPDATE yp_agent_run SET active_thread_id = thread_id WHERE state IN "
            "('queued', 'running', 'waiting_for_approval', 'cancel_requested')"
        )
    )
    with op.batch_alter_table("yp_agent_run") as batch_op:
        batch_op.create_unique_constraint("uq_yp_agent_run_active_thread_id", ["active_thread_id"])


def downgrade() -> None:
    """移除活动线程唯一约束及占用标记。"""
    with op.batch_alter_table("yp_agent_run") as batch_op:
        batch_op.drop_constraint("uq_yp_agent_run_active_thread_id", type_="unique")
        batch_op.drop_column("active_thread_id")
