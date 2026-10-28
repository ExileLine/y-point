# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : pagination.py

from pydantic import BaseModel, Field


class CommonPage(BaseModel):
    page: int = Field(default=1, ge=1, description="页码", examples=[1])
    size: int = Field(default=20, ge=1, le=200, description="每页数量", examples=[20])


def page_size(page: int, size: int) -> tuple:
    """根据页码和每页数量返回查询偏移量与条数。"""
    return (page - 1) * size, size


def query_result(records: list, now_page: int, total: int) -> dict:
    """将分页记录、当前页码和总数组装为查询结果。"""
    res = {
        "records": records,
        "now_page": now_page,
        "total": total,
    }
    return res
