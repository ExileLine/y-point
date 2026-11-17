# -*- coding: utf-8 -*-
# @Time    : 2026-11-17 10:03:50
# @Author  : yangyuexiong
# @File    : 057_ag_ui_event_details.py

"""保存 AG-UI 投影所需的公开模型片段、审批与交付元数据。"""

from alembic import op
import sqlalchemy as sa

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展规范事件源，保持原事件序号和事务边界。"""
    op.add_column("yp_agent_run_message", sa.Column("ag_ui_message_id", sa.String(128), nullable=True, comment="公开 AG-UI 消息 ID"))
    op.add_column("yp_run_approval", sa.Column("ag_ui_run_id", sa.String(128), nullable=True, comment="审批恢复协议运行 ID"))
    op.add_column("yp_agent_run", sa.Column("ag_ui_run_id", sa.String(128), nullable=True, comment="首轮 AG-UI 协议运行 ID"))
    op.add_column("yp_agent_run_event", sa.Column("details", sa.JSON(), nullable=True, comment="版本化公开事件结构数据"))


def downgrade() -> None:
    """移除公开事件结构，保留运行、消息和旧事件。"""
    with op.batch_alter_table("yp_agent_run_message") as batch:
        batch.drop_column("ag_ui_message_id")
    with op.batch_alter_table("yp_run_approval") as batch:
        batch.drop_column("ag_ui_run_id")
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.drop_column("ag_ui_run_id")
    with op.batch_alter_table("yp_agent_run_event") as batch:
        batch.drop_column("details")
