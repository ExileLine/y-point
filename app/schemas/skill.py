# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 10:33:31
# @Author  : yangyuexiong
# @File    : skill.py

"""Skill 管理接口结构。"""

from pydantic import BaseModel, ConfigDict, Field


class SkillCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: dict[str, str] = Field(
        description="相对路径与 UTF-8 内容映射",
        examples=[
            {
                "SKILL.md": "---\nname: example\ndescription: 示例\n---\n",
            }
        ],
    )
    category: str | None = Field(default=None, max_length=64, description="Skill 分组", examples=["写作"])
    visibility: str = Field(default="private", pattern="^(private|public)$", description="私有或公开", examples=["private"])


class SkillUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="预期当前版本号", examples=[1])
    files: dict[str, str] | None = Field(
        default=None,
        description="新版 Skill 完整文件映射",
        examples=[
            {
                "SKILL.md": "---\nname: example\ndescription: 示例\n---\n",
            }
        ],
    )
    category: str | None = Field(default=None, max_length=64, description="Skill 分组", examples=["写作"])
    visibility: str | None = Field(default=None, pattern="^(private|public)$", description="私有或公开", examples=["public"])
    enabled: bool | None = Field(default=None, description="是否启用", examples=[True])
