# -*- coding: utf-8 -*-
# @Time    : 2026-10-24 22:45:00
# @Author  : yangyuexiong
# @File    : 026_answer_artifact_publication.py

"""为文本运行增加自动发布最终回答产物的可选配置。

Revision ID: 026
Revises: 025
"""

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """旧运行不自动发布文件。"""
    op.add_column(
        "yp_agent_run",
        sa.Column("publish_answer_artifact", sa.Boolean(), nullable=False, server_default="0", comment="是否将最终回答发布为文本产物"),
    )


def downgrade() -> None:
    """移除自动发布开关。"""
    op.drop_column("yp_agent_run", "publish_answer_artifact")
