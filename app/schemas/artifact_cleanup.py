# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 15:49:22
# @Author  : yangyuexiong
# @File    : artifact_cleanup.py

"""开发环境文件盘点与物理清理的请求及响应契约。"""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.enums import ArtifactCleanupResult, ArtifactOrigin, ArtifactState

CleanupFilename = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]


class ArtifactCleanupIn(BaseModel):
    """只接受文件原名或盘点接口返回的随机磁盘名，不接受路径。"""

    model_config = ConfigDict(extra="forbid")

    filenames: list[CleanupFilename] = Field(
        min_length=1,
        max_length=100,
        description="待物理删除的文件名称，优先使用 storage_name；原名须在本人文件中唯一，每批最多 100 项",
        examples=[["0123456789abcdef0123456789abcdef", "施工审查报告.docx"]],
    )

    @field_validator("filenames")
    @classmethod
    def validate_filenames(cls, values: list[str]) -> list[str]:
        """拒绝重复名称、路径和空白名称，避免批量操作目标不明确。"""
        if len(values) != len(set(values)):
            raise ValueError("文件名称不能重复")
        if any(not name.strip() or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name for name in values):
            raise ValueError("只允许文件名称，不接受路径或空白名称")
        return values


class ArtifactCleanupFileOut(BaseModel):
    """只在开发环境向文件所属管理员展示磁盘名称。"""

    artifact_id: str = Field(description="文件记录 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="原始文件名称", examples=["施工审查报告.docx"])
    storage_name: str = Field(description="磁盘上的 32 位随机存储名称，不是内容哈希", examples=["0123456789abcdef0123456789abcdef"])
    sha256: str = Field(description="文件内容 SHA-256 摘要", examples=["a" * 64])
    source: ArtifactOrigin = Field(description="上传文件或生成产物", examples=["upload"])
    state: ArtifactState = Field(description="文件记录状态，包含已撤销及待发布的历史文件", examples=["ready"])
    is_deleted: bool = Field(description="是否已逻辑删除", examples=[False])
    byte_size: int = Field(description="数据库记录的文件大小，单位字节", examples=[34335])
    exists_on_disk: bool = Field(description="对应磁盘路径是否为普通文件；符号链接不视为可用正文", examples=[True])
    run_id: str | None = Field(description="生成产物所属 Run；上传文件通常为空", examples=[None])
    expires_at: datetime | None = Field(description="文件到期时间，空值表示未设置", examples=[None])
    create_time: datetime = Field(description="文件记录创建时间", examples=["2026-11-10T15:00:00"])


class ArtifactCleanupPageOut(BaseModel):
    """按文件记录去重的盘点分页，包含缺失及已撤销文件。"""

    items: list[ArtifactCleanupFileOut] = Field(description="本页上传物和生成产物", examples=[[]])
    total: int = Field(description="匹配范围内去重后的文件总数", examples=[12])
    page: int = Field(description="当前页码", examples=[1])
    page_size: int = Field(description="每页最多返回数量", examples=[100])


class ArtifactCleanupItemOut(BaseModel):
    """逐项说明文件是否完成物理删除，失败项可再次提交。"""

    artifact_id: str = Field(description="文件记录 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    filename: str = Field(description="原始文件名称", examples=["施工审查报告.docx"])
    storage_name: str = Field(description="实际处理的随机磁盘名称", examples=["0123456789abcdef0123456789abcdef"])
    result: ArtifactCleanupResult = Field(description="删除结果：已删除、原文件已缺失或物理删除失败", examples=["deleted"])


class ArtifactCleanupOut(BaseModel):
    """所有目标先撤销数据库访问，再返回不可回滚的逐文件物理删除结果。"""

    items: list[ArtifactCleanupItemOut] = Field(description="按请求顺序返回的逐文件结果，重复指向同一文件的名称只处理一次", examples=[[]])
    deleted_count: int = Field(description="实际删除文件数", examples=[2])
    already_missing_count: int = Field(description="磁盘文件此前已不存在的数量", examples=[1])
    failed_count: int = Field(description="物理删除失败的数量；记录已撤销访问，可用 storage_name 重试", examples=[0])
