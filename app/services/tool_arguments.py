# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 01:41:53
# @Author  : yangyuexiong
# @File    : tool_arguments.py

"""提供执行前参数校验及只读脚本主动拒绝输入的受控反馈。"""

import json
from itertools import islice

from jsonschema import Draft202012Validator
from langchain_core.tools import ToolException

from app.core.enums import ToolFeedbackFailure

MAX_ARGUMENT_ISSUES = 8


class ScriptInputToolError(ToolException):
    """只读脚本主动拒绝输入后的有界反馈，脚本已经执行。"""


class ToolArgumentsError(ToolException):
    """仅表示尚未执行工具的参数结构错误，不包含上游或脚本运行异常。"""


def validate_tool_arguments(schema: dict, arguments: dict) -> None:
    """严格执行原 Schema，错误只返回字段路径、规则和缺少的字段名。"""
    errors = list(islice(Draft202012Validator(schema).iter_errors(arguments), MAX_ARGUMENT_ISSUES + 1))
    if not errors:
        return
    issues = []
    for error in errors[:MAX_ARGUMENT_ISSUES]:
        issue = {
            "path": "/" + "/".join(str(part)[:80].replace("~", "~0").replace("/", "~1") for part in error.absolute_path)[:256],
            "rule": str(error.validator)[:64],
        }
        if error.validator == "required" and isinstance(error.instance, dict):
            issue["missing_fields"] = [str(field)[:80] for field in error.validator_value if field not in error.instance][:MAX_ARGUMENT_ISSUES]
        issues.append(issue)
    raise ToolArgumentsError(
        json.dumps(
            {
                "error": ToolFeedbackFailure.INVALID_ARGUMENTS,
                "message": "工具尚未执行。请根据工具输入 Schema 补齐或修正参数，使用新的调用 ID 再调用；不要编造缺失的业务数据。",
                "retryable": True,
                "issues": issues,
                "truncated": len(errors) > MAX_ARGUMENT_ISSUES,
            },
            ensure_ascii=False,
        ),
    )


def tool_argument_error_content(error: ToolException) -> str:
    """仅将受控参数或只读脚本拒绝反馈给模型，其余工具异常沿用原失败处理。"""
    if not isinstance(error, (ToolArgumentsError, ScriptInputToolError)):
        raise error
    return str(error)


def is_tool_argument_error(content: object) -> bool:
    """识别已转换的有界参数错误，以便审计独立保留该失败类别。"""
    if not isinstance(content, str) or len(content) > 16 * 1024:
        return False
    try:
        payload = json.loads(content)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and payload.get("error") == ToolFeedbackFailure.INVALID_ARGUMENTS and payload.get("retryable") is True
