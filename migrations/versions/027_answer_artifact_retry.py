# -*- coding: utf-8 -*-
# @Time    : 2026-10-25 13:00:00
# @Author  : yangyuexiong
# @File    : 027_answer_artifact_retry.py

"""为回答产物补发失败增加持久化退避时间。

Revision ID: 027
Revises: 026
"""

import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("yp_agent_run", sa.Column(
        "answer_artifact_retry_at", sa.DateTime(timezone=True), nullable=True, comment="回答产物下次补发时间",
    ))


def downgrade() -> None:
    op.drop_column("yp_agent_run", "answer_artifact_retry_at")
