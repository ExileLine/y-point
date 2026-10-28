# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : custom_exception.py

from typing import Any

from fastapi import HTTPException


class CustomException(HTTPException):
    def __init__(self, status_code: int = 200, detail: str = "出现异常", custom_code: int = 200, data: Any = None) -> None:
        """保存业务错误码、HTTP 状态和可选响应数据。"""
        super().__init__(status_code=status_code, detail=detail)
        self.custom_code = custom_code
        self.data = data
