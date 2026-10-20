# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : common.py

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CommonPydanticCreate(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )

    remark: Optional[str] = Field(default=None, description="备注", examples=[""])
    creator: Optional[str] = Field(default=None, description="创建人(不需要传,从鉴权获取)")
    creator_id: Optional[int] = Field(default=None, description="创建人ID(不需要传,从鉴权获取)")

    def dict(self, **kwargs):
        return super().model_dump(exclude_unset=True, **kwargs)


class CommonPydanticUpdate(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
    )

    id: Optional[int] = Field(default=None, description="需要编辑的数据ID", examples=[1])
    remark: Optional[str] = Field(default=None, description="备注", examples=[""])
    modifier: Optional[str] = Field(default=None, description="更新人(不需要传,从鉴权获取)")
    modifier_id: Optional[int] = Field(default=None, description="更新人ID(不需要传,从鉴权获取)")

    def dict(self, **kwargs):
        return super().model_dump(exclude_unset=True, exclude_defaults=True, exclude_none=True, **kwargs)