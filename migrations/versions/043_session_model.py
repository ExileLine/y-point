# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 17:26:34
# @Author  : yangyuexiong
# @File    : 043_session_model.py

"""会话模型与 Agent 默认模型解耦，并回填已有会话。"""

from alembic import op
import sqlalchemy as sa

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """优先沿用最近成功模型，再沿用最近运行或 Agent 默认模型。"""
    op.add_column(
        "yp_agent_session",
        sa.Column("model_id", sa.Integer(), nullable=True, comment="会话当前模型 ID；旧记录为空时从历史回填"),
    )
    op.execute(
        sa.text(
            """
        UPDATE yp_agent_session SET model_id = COALESCE(
            (SELECT r.model_id FROM yp_agent_run r
             WHERE r.session_id = yp_agent_session.id AND r.owner_id = yp_agent_session.owner_id
               AND r.state = 'completed' AND r.model_id IS NOT NULL
             ORDER BY r.create_time DESC, r.id DESC LIMIT 1),
            (SELECT r.model_id FROM yp_agent_run r
             WHERE r.session_id = yp_agent_session.id AND r.owner_id = yp_agent_session.owner_id AND r.model_id IS NOT NULL
             ORDER BY r.create_time DESC, r.id DESC LIMIT 1),
            (SELECT a.default_model_id FROM yp_agent_config a WHERE a.id = yp_agent_session.agent_id)
        )
    """,
        ),
    )


def downgrade() -> None:
    """撤销会话模型字段，保留历史运行模型快照。"""
    op.drop_column("yp_agent_session", "model_id")
