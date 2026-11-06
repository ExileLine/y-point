# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:03:07
# @Author  : yangyuexiong
# @File    : skill_package.py

"""Skill 草稿编辑、发布、版本恢复及脚本试运行契约。"""

from pydantic import BaseModel, ConfigDict, Field
from app.core.enums import Visibility


class SkillPackageCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64, description="Skill 目录名称", examples=["contract-review"])
    description: str = Field(min_length=1, max_length=1024, description="Skill 适用场景", examples=["审查合同并生成报告"])


class SkillDraftStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_version: int | None = Field(default=None, ge=1, description="从指定历史版本创建草稿", examples=[2])
    expected_revision: int | None = Field(default=None, ge=1, description="重置已有草稿时必须匹配的修订号", examples=[1])


class SkillDraftUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, description="预期草稿修订号", examples=[1])
    files: dict[str, str] = Field(default_factory=dict, description="新增或替换的 UTF-8 文本文件", examples=[{}])
    delete_paths: list[str] = Field(default_factory=list, max_length=128, description="移除的文件路径", examples=[["README.md"]])
    renames: dict[str, str] = Field(default_factory=dict, description="原路径到新路径的重命名映射", examples=[{}])
    category: str | None = Field(default=None, max_length=64, description="发布后的分组", examples=["审查"])
    visibility: Visibility | None = Field(default=None, description="发布后的可见性", examples=["private"])
    enabled: bool | None = Field(default=None, description="发布后是否启用", examples=[True])


class SkillDraftRevisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1, description="预期草稿修订号", examples=[2])


class SkillScriptTestIn(SkillDraftRevisionIn):
    path: str = Field(max_length=255, description="待验证的包内脚本路径", examples=["scripts/validate.py"])
    arguments: dict = Field(default_factory=dict, description="符合脚本 Schema 的试运行输入", examples=[{}])
