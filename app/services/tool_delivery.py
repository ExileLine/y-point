# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 10:00:00
# @Author  : yangyuexiong
# @File    : tool_delivery.py

"""标记有明确证据尚未发送业务调用的失败，不推断远端写入结果。"""


class ToolNotSentError(ValueError):
    """本地预检或协议握手失败，业务 tools/call 尚未提交。"""


class ToolNotSentTimeoutError(ToolNotSentError, TimeoutError):
    """业务请求发送前超时，同时保留超时类别。"""


def mark_request_not_sent(error: BaseException) -> None:
    """给原始取消异常附加平台证据，保留 asyncio 对异常类型和取消计数的识别。"""
    error._ypoint_request_not_sent = True


def request_not_sent(error: BaseException) -> bool:
    """仅信任平台自己的未发送标记，混合任务组异常不推断为未发送。"""
    # asyncio.wait_for 会将发送前取消标记包装成 TimeoutError，证据不能因此丢失。
    if isinstance(error, TimeoutError) and error.__cause__ is not None:
        return request_not_sent(error.__cause__)
    if getattr(error, "_ypoint_request_not_sent", False) is True:
        return True
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(request_not_sent(item) for item in error.exceptions)
    return isinstance(error, ToolNotSentError)
