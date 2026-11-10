# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : artifact.py

"""文件上传和授权查询的公开数据结构。"""

from datetime import datetime

from app.core.enums import ArtifactOrigin, ArtifactPreviewKind, ArtifactState

from pydantic import BaseModel, ConfigDict, Field


class ArtifactOut(BaseModel):
    """不暴露私有存储键的文件元数据。"""

    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="文件 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="原始文件名", examples=["报告.docx"])
    media_type: str = Field(description="服务端核验后的媒体类型", examples=["application/pdf"])
    byte_size: int = Field(description="文件字节数", examples=[2048])
    sha256: str = Field(description="文件 SHA-256 摘要", examples=["a" * 64])
    source: ArtifactOrigin = Field(description="文件来源", examples=["upload"])
    state: ArtifactState = Field(description="文件状态", examples=["ready"])
    run_id: str | None = Field(description="关联的运行 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    source_artifact_id: str | None = Field(description="生成产物的来源文件 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    context_artifact_ids: list[str] = Field(
        default_factory=list,
        description="本轮提供给模型的参考文件 ID；不代表模型实际引用的段落",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )
    read_source_artifact_ids: list[str] = Field(
        default_factory=list,
        description="生成前成功读取的旧产物 ID；不证明实际引用",
        examples=[["550e8400-e29b-41d4-a716-446655440001"]],
    )
    template_source: dict | None = Field(default=None, description="报告使用的 Skill 版本及模板摘要，不含存储路径", examples=[None])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-23T15:00:00"])


class ArtifactReferenceOut(BaseModel):
    """消息中的历史文件引用；下载仍使用文件授权接口。"""

    artifact_id: str = Field(description="私有文件 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="提交时文件名", examples=["notes.txt"])
    media_type: str = Field(description="提交时文件类型", examples=["text/plain"])
    byte_size: int = Field(description="提交时文件大小", examples=[1024])
    available: bool = Field(description="文件元数据当前是否有效；下载仍须逐次校验", examples=[True])
    included_in_context: bool = Field(default=False, description="提交时是否选择将可提取内容送入本轮模型上下文", examples=[False])


class GeneratedArtifactReferenceOut(BaseModel):
    """助手消息关联的生成产物；下载仍需逐次授权。"""

    template_source: dict | None = Field(default=None, description="报告使用的固定 Skill 版本及模板摘要", examples=[None])

    artifact_id: str = Field(description="生成产物 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="产物文件名", examples=["报告.docx"])
    media_type: str = Field(description="服务端核验后的文件类型", examples=["application/vnd.openxmlformats-officedocument.wordprocessingml.document"])
    byte_size: int = Field(description="文件字节数", examples=[2048])
    available: bool = Field(description="产物当前是否有效；下载时仍需重新授权", examples=[True])
    context_artifact_ids: list[str] = Field(
        default_factory=list,
        description="本轮提供给模型的参考文件 ID；不代表模型实际引用的段落",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )
    read_source_artifact_ids: list[str] = Field(
        default_factory=list,
        description="生成前成功读取的旧产物 ID；不证明实际引用",
        examples=[["550e8400-e29b-41d4-a716-446655440001"]],
    )


class ArtifactPreviewSheet(BaseModel):
    """预览中的有界工作表，不计算公式或读取外部链接。"""

    name: str = Field(description="工作表名称", examples=["销售统计"])
    rows: list[list[str]] = Field(description="按行排列的展示文本", examples=[[["月份", "金额"], ["十一月", "100"]]])


class ArtifactPreviewOut(BaseModel):
    """实时授权后的文件内容预览，不公开存储键或路径。"""

    artifact_id: str = Field(description="文件 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="服务端记录的文件名", examples=["差旅报告.docx"])
    media_type: str = Field(description="服务端核验的原文件类型", examples=["application/pdf"])
    kind: ArtifactPreviewKind = Field(description="内容展示方式", examples=["html"])
    content: str = Field(default="", description="文本、Markdown 或已清洗的静态 HTML", examples=["差旅报告正文"])
    sheets: list[ArtifactPreviewSheet] = Field(default_factory=list, description="CSV 或 XLSX 的有界表格内容", examples=[[]])
    truncated: bool = Field(default=False, description="是否因预览限额只显示了部分内容", examples=[False])
    notice: str | None = Field(default=None, description="格式与版式的预览限制说明", examples=["内容预览不保留原文件的完整排版"])
