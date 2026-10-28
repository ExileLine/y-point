# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : response.py

from typing import Any

from fastapi.responses import JSONResponse

from app.utils.time_tools import TimeTools


def custom_http_dict(custom_code):
    """根据业务码查找默认中文响应消息，未知编码返回空值。"""
    code_dict = {
        200: "操作成功",
        201: "创建成功",
        203: "编辑成功",
        204: "删除成功",
        401: "未授权",
        500: "服务器异常",
        10001: "必传参数",
        10002: "未找到数据",
        10003: "唯一校验",
        10004: "参数类型错误",
        10005: "业务校验错误",
        10006: "请求参数错误",
        10007: "未公开使用，非创建人，无法修改。",
    }

    message = code_dict.get(custom_code)
    if message:
        return message

    return None


def api_response(
    http_code: int = 200, code: int = 200, message: str = None, data: Any = None, datetime_format: bool = True, is_pop: bool = True
) -> JSONResponse:
    """构建统一 JSON 响应，并按选项格式化时间或省略空数据。"""
    if not message:
        message = custom_http_dict(code)

    if data and isinstance(data, dict) and datetime_format:
        if data.get("create_time"):
            data["create_time"] = TimeTools.convert_to_standard_format(data["create_time"])

        if data.get("update_time"):
            data["update_time"] = TimeTools.convert_to_standard_format(data["update_time"])

    content = {"code": code, "message": message, "data": data}

    if not content.get("data") and is_pop:
        content.pop("data")

    return JSONResponse(status_code=http_code, content=content)
