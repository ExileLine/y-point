# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 10:33:31
# @Author  : yangyuexiong
# @File    : skill.py

"""Skill 包、不可变版本及 Agent/Run 关联。"""

from sqlalchemy import Boolean, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class Skill(CustomBaseModel):
    __table_name__ = "skill"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_yp_skill_owner_name"),
        {
            "comment": "用户 Skill 表",
        },
    )

    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="创建用户 ID")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="Skill 唯一名称")
    description: Mapped[str] = mapped_column(String(1024), nullable=False, comment="Skill 简介")
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="分组名称")
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, comment="私有或公开")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="当前版本号")


class SkillVersion(CustomBaseModel):
    __table_name__ = "skill_version"
    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uq_yp_skill_version_number"),
        {
            "comment": "Skill 不可变版本表",
        },
    )

    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="Skill ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="包内容 SHA256")
    files: Mapped[dict] = mapped_column(JSON, nullable=False, comment="UTF-8 文件路径与内容映射")
    manifest: Mapped[list] = mapped_column(JSON, nullable=False, comment="文件清单")


class AgentSkill(CustomBaseModel):
    __table_name__ = "agent_skill"
    __table_args__ = (
        UniqueConstraint("agent_id", "skill_id", name="uq_yp_agent_skill_pair"),
        {
            "comment": "智能体关联 Skill 版本表",
        },
    )

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="智能体 ID")
    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Skill ID")
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="固定 Skill 版本号")


class RunSkill(CustomBaseModel):
    __table_name__ = "run_skill"
    __table_args__ = (
        UniqueConstraint("run_id", "skill_id", name="uq_yp_run_skill_pair"),
        {
            "comment": "运行使用的 Skill 版本表",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    skill_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Skill ID")
    skill_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="运行固定版本号")
