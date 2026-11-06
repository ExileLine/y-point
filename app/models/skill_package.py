# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:03:07
# @Author  : yangyuexiong
# @File    : skill_package.py

"""Skill 工作区草稿、不可变文件关联和已验证的包内执行绑定。"""

from sqlalchemy import Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class SkillDraft(CustomBaseModel):
    """每个 Skill 保留一个由修订号保护的可变工作区。"""

    __table_name__ = "skill_draft"
    __table_args__ = (
        UniqueConstraint("skill_id", name="uq_yp_skill_draft_skill"),
        {
            "comment": "Skill 文件包编辑草稿表",
        },
    )

    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属 Skill ID")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="草稿并发修订号")
    base_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="编辑基于的当前发布版本")
    files: Mapped[dict] = mapped_column(JSON, nullable=False, comment="逻辑路径到不可变文件资源的映射")
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, comment="待发布的分组可见性和启用设置")


class SkillVersionFile(CustomBaseModel):
    """一个发布版本内的逻辑文件对应一个不可变私有文件。"""

    __table_name__ = "skill_version_file"
    __table_args__ = (
        UniqueConstraint("skill_id", "skill_version", "path", name="uq_yp_skill_version_file_path"),
        {
            "comment": "Skill 发布版本文件关联表",
        },
    )

    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属 Skill ID")
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属发布版本")
    path: Mapped[str] = mapped_column(String(255), nullable=False, comment="包内规范化相对文件路径")
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="不可变私有文件资源 ID")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, comment="发布时固定的文件摘要")


class SkillScript(CustomBaseModel):
    """按整个包摘要固定脚本试运行、依赖环境及 Tool 审计入口。"""

    __table_name__ = "skill_script"
    __table_args__ = (
        UniqueConstraint("skill_id", "package_hash", "path", name="uq_yp_skill_script_package_path"),
        {
            "comment": "Skill 文件包脚本执行绑定表",
        },
    )

    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属 Skill ID")
    package_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="试运行时固定的完整包摘要")
    path: Mapped[str] = mapped_column(String(255), nullable=False, comment="包内脚本路径")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="托管的内部 Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="固定的内部 Tool 版本")
