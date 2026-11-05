# -*- coding: utf-8 -*-
# @Time    : 2026-11-05 11:43:38
# @Author  : yangyuexiong
# @File    : 045_skill_multi_templates.py

"""将 Skill 单模板关联扩展为版本内按文件名唯一的多模板资源。"""

import unicodedata

from alembic import op
import sqlalchemy as sa

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """回填历史模板文件名，并以资源名扩展唯一约束，不改变历史引用。"""
    op.add_column(
        "yp_skill_template",
        sa.Column("template_key", sa.String(255), nullable=True, comment="版本内唯一的规范化模板文件名"),
    )
    templates = sa.table("yp_skill_template", sa.column("id"), sa.column("artifact_id"), sa.column("template_key"))
    artifacts = sa.table("yp_artifact", sa.column("id"), sa.column("filename"))
    connection = op.get_bind()
    records = connection.execute(
        sa.select(templates.c.id, artifacts.c.filename).select_from(
            templates.outerjoin(artifacts, templates.c.artifact_id == artifacts.c.id)
        )
    ).all()
    for identifier, filename in records:
        key = unicodedata.normalize("NFC", filename or "report-template.docx").casefold()
        connection.execute(sa.update(templates).where(templates.c.id == identifier).values(template_key=key))
    with op.batch_alter_table("yp_skill_template") as batch:
        batch.drop_constraint("uq_yp_skill_template_version", type_="unique")
        batch.alter_column("template_key", existing_type=sa.String(255), nullable=False)
        batch.create_unique_constraint("uq_yp_skill_template_resource", ["skill_id", "skill_version", "template_key"])


def downgrade() -> None:
    """只在可无损恢复单模板约束时降级，禁止静默丢弃多模板记录。"""
    connection = op.get_bind()
    templates = sa.table("yp_skill_template", sa.column("skill_id"), sa.column("skill_version"))
    multiple = connection.execute(
        sa.select(templates.c.skill_id).group_by(templates.c.skill_id, templates.c.skill_version).having(sa.func.count() > 1).limit(1)
    ).first()
    if multiple is not None:
        raise RuntimeError("存在多模板 Skill 版本，不能无损降级为 044；请保留 045 或恢复升级前备份")
    with op.batch_alter_table("yp_skill_template") as batch:
        batch.drop_constraint("uq_yp_skill_template_resource", type_="unique")
        batch.create_unique_constraint("uq_yp_skill_template_version", ["skill_id", "skill_version"])
        batch.drop_column("template_key")
