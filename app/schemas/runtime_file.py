# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 15:27:04
# @Author  : yangyuexiong
# @File    : runtime_file.py

"""运行内文本虚拟文件读取参数，不开放媒体或宿主文件选项。"""

from pydantic import BaseModel, Field


class RuntimeTextFileReadIn(BaseModel):
    """只描述平台授权的 Skill 文本和当前会话历史分页。"""

    file_path: str = Field(min_length=1, description="已授权的 Skill 文件或当前会话历史文件路径", examples=["/skills/review/SKILL.md"])
    offset: int = Field(default=0, ge=0, description="零基行号；历史文件采用响应说明的软换行视图", examples=[0])
    limit: int = Field(default=2000, gt=0, description="期望读取行数；历史文件还受每页字符预算约束", examples=[10])
