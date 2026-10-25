# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 15:24:18
# @Author  : yangyuexiong
# @File    : artifact.py

"""私有文件及运行产物的元数据。"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class Artifact(CustomBaseModel):
    """保存文件授权信息，文件正文由私有存储管理。"""

    __table_name__ = "artifact"
    __enable_auto_id__ = False
    __table_args__ = (
        UniqueConstraint("run_id", "publication_key", name="uq_yp_artifact_run_publication_key"),
        {"comment": "用户附件与运行产物表"},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, comment="文件 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="文件所属用户 ID")
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True, comment="关联运行 ID")
    source_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="生成产物的来源文件 ID")
    publication_key: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="同一运行内的产物发布幂等键")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, comment="展示用原始文件名")
    media_type: Mapped[str] = mapped_column(String(128), nullable=False, comment="校验后的文件类型")
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="文件字节数")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, comment="文件 SHA-256 摘要")
    storage_key: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, comment="随机私有存储键")
    source: Mapped[str] = mapped_column(String(16), nullable=False, comment="文件来源：upload 或 generated")
    state: Mapped[str] = mapped_column(String(16), nullable=False, comment="文件状态：ready 或 deleted")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="文件到期时间")
