# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : pagination.py

from pydantic import BaseModel, ConfigDict, Field


class CommonPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    page: int = Field(default=1, ge=1, description="页码")
    size: int = Field(default=20, ge=1, le=200, description="每页数量")


def page_size(page: int, size: int) -> tuple:
    return (page - 1) * size, size


def query_result(records: list, now_page: int, total: int) -> dict:
    res = {
        "records": records,
        "now_page": now_page,
        "total": total,
    }
    return res