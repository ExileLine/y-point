# -*- coding: utf-8 -*-
# @Time    : 2026-11-24 23:27:00
# @Author  : yangyuexiong
# @File    : 067_ag_ui_public_history.py

"""缓存终态 Run 的公开消息；历史数据按需重建，不改动事件和会话版本。"""

from alembic import op
import sqlalchemy as sa

revision = "067"
down_revision = "066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增可丢弃的派生缓存，保留原事件作为唯一事实来源。"""
    op.create_table(
        "yp_agent_run_public_history",
        sa.Column("run_id", sa.String(36), primary_key=True, comment="所属业务 Run"),
        sa.Column("event_seq", sa.Integer(), nullable=False, comment="公开消息覆盖的源事件上界"),
        sa.Column("projection_version", sa.Integer(), nullable=False, comment="公开投影实现版本"),
        sa.Column("messages", sa.JSON(), nullable=False, comment="仅从公开事件投影得到的协议消息"),
        comment="运行公开消息投影缓存",
    )


def downgrade() -> None:
    """缓存可以重新生成，删除缓存不丢失会话或运行事实。"""
    op.drop_table("yp_agent_run_public_history")
