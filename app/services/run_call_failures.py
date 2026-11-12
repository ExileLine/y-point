# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 22:52:06
# @Author  : yangyuexiong
# @File    : run_call_failures.py

"""将调用异常归类为公开原因，禁止保存异常正文、工具输入或远端输出。"""

import asyncio
import json

import httpx
from langchain_core.tools import ToolException

from app.core.enums import RunCallFailure, ToolFeedbackFailure
from app.services.script_sandbox import SandboxConfigurationError
from app.services.tool_arguments import ScriptInputToolError, ToolArgumentsError

FAILURE_MESSAGES = {
    RunCallFailure.TIMEOUT: "调用超过单次执行时限，请检查工具或 MCP 的响应时间与超时设置。",
    RunCallFailure.INVALID_ARGUMENTS: "调用参数不符合工具输入规范，工具未执行；模型可修正参数后再次调用。",
    RunCallFailure.INPUT_REJECTED: "脚本已运行，但主动拒绝了本次输入，请检查输入资料是否完整且符合要求。",
    RunCallFailure.ARTIFACT_UNAVAILABLE: "需要读取的生成文件不可用，可能尚未发布、已过期或已无访问权限。",
    RunCallFailure.PERMISSION_DENIED: "调用被拒绝，请检查当前用户对工具、Skill 或文件的访问权限。",
    RunCallFailure.CONNECTION_FAILED: "无法连接工具服务，请检查服务状态和网络连接。",
    RunCallFailure.AUTHENTICATION_FAILED: "工具服务认证失败或拒绝访问，请检查工具连接配置与凭据。",
    RunCallFailure.RATE_LIMITED: "工具服务触发请求频率限制，请稍后重新提交。",
    RunCallFailure.REMOTE_ERROR: "远端工具服务返回错误，请检查服务状态与工具版本。",
    RunCallFailure.CONFIGURATION_ERROR: "脚本依赖环境或沙箱镜像不可用，请检查工具配置并重新准备环境。",
    RunCallFailure.SCRIPT_FAILED: "脚本进程执行失败，请检查脚本逻辑及所需依赖。",
    RunCallFailure.INVALID_OUTPUT: "脚本返回内容不是有效 JSON，请检查脚本的标准输出格式。",
    RunCallFailure.RESPONSE_LIMIT: "工具返回内容超过允许的大小，请缩小查询范围或减少输出。",
    RunCallFailure.TOOL_CHANGED: "MCP 工具结构已变化，请更新并发布工具版本后重新提交。",
    RunCallFailure.EXECUTION_ERROR: "工具执行失败，未提供可安全展示的具体原因，请检查工具配置或联系维护者。",
    RunCallFailure.INTERRUPTED: "调用执行被中断，无法确认最终执行结果，请核实后再决定是否重新提交。",
}


def call_failure_message(code: str | None) -> str:
    """仅从固定公开映射读取文案，旧事件或未知代码不透传。"""
    return FAILURE_MESSAGES.get(code, "本次调用失败，历史记录未保存具体原因。")


def tool_result_failure(content: object) -> RunCallFailure:
    """仅识别受控结构的错误类别，不公开或存储返回内容。"""
    if isinstance(content, str) and len(content) <= 16 * 1024:
        try:
            payload = json.loads(content)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            code = payload.get("error")
            if isinstance(code, str) and code in set(ToolFeedbackFailure):
                return RunCallFailure(code)
    return RunCallFailure.EXECUTION_ERROR


def tool_exception_failure(error: BaseException) -> RunCallFailure:
    """按异常类型生成原因码，任务组包装只在原因一致时解包。"""
    if isinstance(error, BaseExceptionGroup):
        reasons = {tool_exception_failure(item) for item in error.exceptions}
        return next(iter(reasons)) if len(reasons) == 1 else RunCallFailure.EXECUTION_ERROR
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return RunCallFailure.TIMEOUT
    if isinstance(error, asyncio.CancelledError):
        return RunCallFailure.INTERRUPTED
    if isinstance(error, PermissionError):
        return RunCallFailure.PERMISSION_DENIED
    if isinstance(error, ToolArgumentsError):
        return RunCallFailure.INVALID_ARGUMENTS
    if isinstance(error, ScriptInputToolError):
        return RunCallFailure.INPUT_REJECTED
    if isinstance(error, ToolException):
        return tool_result_failure(str(error))
    if isinstance(error, SandboxConfigurationError):
        return RunCallFailure.CONFIGURATION_ERROR
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 403):
            return RunCallFailure.AUTHENTICATION_FAILED
        return RunCallFailure.RATE_LIMITED if status == 429 else RunCallFailure.REMOTE_ERROR
    if isinstance(error, (httpx.TransportError, ConnectionError)):
        return RunCallFailure.CONNECTION_FAILED
    if isinstance(error, ValueError):
        known = {
            "脚本执行失败": RunCallFailure.SCRIPT_FAILED,
            "脚本必须输出一个 JSON 值": RunCallFailure.INVALID_OUTPUT,
            "脚本输出超过 64 KiB": RunCallFailure.RESPONSE_LIMIT,
            "MCP 结果超过 64 KiB": RunCallFailure.RESPONSE_LIMIT,
            "MCP 传输超过接收字节上限": RunCallFailure.RESPONSE_LIMIT,
            "MCP 工具结构已变化，请发布新版本": RunCallFailure.TOOL_CHANGED,
            "MCP 工具执行失败": RunCallFailure.REMOTE_ERROR,
            "MCP 凭据未授权共享": RunCallFailure.PERMISSION_DENIED,
            "Tool 授权已失效": RunCallFailure.PERMISSION_DENIED,
        }
        return known.get(str(error), RunCallFailure.EXECUTION_ERROR)
    return RunCallFailure.EXECUTION_ERROR
