# -*- coding: utf-8 -*-
# @Time    : 2026-11-05 09:56:33
# @Author  : yangyuexiong
# @File    : skill_template.py

"""不可变 Skill 模板资源与字段契约，正文复用私有文件存储。"""

from sqlalchemy import Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class SkillTemplate(CustomBaseModel):
    __table_name__ = "skill_template"
    __table_args__ = (
        UniqueConstraint("skill_id", "skill_version", "template_key", name="uq_yp_skill_template_resource"),
        {
            "comment": "Skill 版本固定的 DOCX 报告模板表",
        },
    )

    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属 Skill ID")
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属不可变 Skill 版本")
    template_key: Mapped[str] = mapped_column(String(255), nullable=False, comment="版本内唯一的规范化模板文件名")
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="私有模板文件 ID")
    fields_schema: Mapped[dict] = mapped_column(JSON, nullable=False, comment="报告数据 JSON Schema 契约")
