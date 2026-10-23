# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:01:51
# @Author  : yangyuexiong
# @File    : 008_shared_checkpoint_thread.py

"""允许同一会话线程承载多个运行。

Revision ID: 008
Revises: 007
"""

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """将线程唯一约束改为普通查询索引。"""
    with op.batch_alter_table("yp_agent_run") as batch_op:
        batch_op.drop_constraint("uq_yp_agent_run_thread_id", type_="unique")
        batch_op.create_index("ix_yp_agent_run_thread_id", ["thread_id"])


def downgrade() -> None:
    """恢复线程唯一约束；已有重复线程的运行需先清理。"""
    with op.batch_alter_table("yp_agent_run") as batch_op:
        batch_op.drop_index("ix_yp_agent_run_thread_id")
        batch_op.create_unique_constraint("uq_yp_agent_run_thread_id", ["thread_id"])
