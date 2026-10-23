# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:57:11
# @Author  : yangyuexiong
# @File    : 016_artifact_publication.py

"""为运行产物添加发布幂等键。

Revision ID: 016
Revises: 015
"""

import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """允许同一运行对同一产物只发布一次。"""
    with op.batch_alter_table("yp_artifact") as batch:
        batch.add_column(sa.Column("publication_key", sa.String(128), nullable=True, comment="同一运行内的产物发布幂等键"))
        batch.create_unique_constraint("uq_yp_artifact_run_publication_key", ["run_id", "publication_key"])


def downgrade() -> None:
    """移除产物发布幂等键。"""
    with op.batch_alter_table("yp_artifact") as batch:
        batch.drop_constraint("uq_yp_artifact_run_publication_key", type_="unique")
        batch.drop_column("publication_key")
