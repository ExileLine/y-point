# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 09:08:24
# @Author  : yangyuexiong
# @File    : run_artifact_read.py

"""记录运行中成功读取过的同会话生成文件。"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class RunArtifactRead(CustomBaseModel):
    __table_name__ = "run_artifact_read"
    __table_args__ = {
        "comment": "运行已读取的旧生成产物",
    }

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="读取发生的运行 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="运行所有者 ID")
    source_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="已读取的旧产物 ID")
