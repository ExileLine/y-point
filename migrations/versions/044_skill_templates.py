# -*- coding: utf-8 -*-
# @Time    : 2026-11-05 09:56:33
# @Author  : yangyuexiong
# @File    : 044_skill_templates.py

"""增加版本化 Skill 报告模板、强制交付标记与产物来源。"""

from alembic import op
import sqlalchemy as sa
from app.models.base import ShanghaiDateTime

revision = "044"
down_revision = "043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加不带外键的模板元数据及兼容既有记录的可选字段。"""
    op.create_table(
        "yp_skill_template",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="所属 Skill ID"),
        sa.Column("skill_version", sa.Integer(), nullable=False, comment="所属不可变 Skill 版本"),
        sa.Column("artifact_id", sa.String(36), nullable=False, comment="私有模板文件 ID"),
        sa.Column("fields_schema", sa.JSON(), nullable=False, comment="报告数据 JSON Schema 契约"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("skill_id", "skill_version", name="uq_yp_skill_template_version"),
        comment="Skill 版本固定的 DOCX 报告模板表",
    )
    op.create_index("ix_yp_skill_template_skill_id", "yp_skill_template", ["skill_id"])
    op.add_column(
        "yp_run_skill",
        sa.Column("required_report", sa.Boolean(), nullable=False, server_default=sa.false(), comment="是否要求按本 Skill 模板交付报告"),
    )
    op.add_column("yp_artifact", sa.Column("template_source", sa.JSON(), nullable=True, comment="生成报告固定的 Skill 与模板来源快照"))


def downgrade() -> None:
    """撤销新增元数据，不删除私有文件或历史对话。"""
    op.drop_column("yp_artifact", "template_source")
    op.drop_column("yp_run_skill", "required_report")
    op.drop_table("yp_skill_template")
