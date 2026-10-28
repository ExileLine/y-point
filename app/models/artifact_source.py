# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 09:08:24
# @Author  : yangyuexiong
# @File    : artifact_source.py

"""保存生成产物与生成前成功读取的旧产物关系。"""

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class ArtifactSource(CustomBaseModel):
    __table_name__ = "artifact_source"
    __table_args__ = (
        UniqueConstraint("target_artifact_id", "source_artifact_id", name="uq_yp_artifact_source_pair"),
        UniqueConstraint("target_artifact_id", "position", name="uq_yp_artifact_source_position"),
        {
            "comment": "模型生成文件与此前读取的旧产物关系",
        },
    )

    target_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="新产物 ID")
    source_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="旧产物 ID")
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="生成运行 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="产物所有者 ID")
    position: Mapped[int] = mapped_column(Integer, nullable=False, comment="首次读取顺序")
