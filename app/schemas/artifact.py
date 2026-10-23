# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : artifact.py

"""文件上传和授权查询的公开数据结构。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ArtifactOut(BaseModel):
    """不暴露私有存储键的文件元数据。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="文件 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="原始文件名", examples=["报告.docx"])
    media_type: str = Field(description="服务端核验后的媒体类型", examples=["application/pdf"])
    byte_size: int = Field(description="文件字节数", examples=[2048])
    sha256: str = Field(description="文件 SHA-256 摘要", examples=["a" * 64])
    source: str = Field(description="文件来源", examples=["upload"])
    state: str = Field(description="文件状态", examples=["ready"])
    run_id: str | None = Field(description="关联的运行 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-23T15:00:00"])
