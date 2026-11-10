# -*- coding: utf-8 -*-
# @Time    : 2026-11-09 16:45:36
# @Author  : yangyuexiong
# @File    : runtime_limits.py

"""运行参数的动态校验与可向已登录用户公开的配置。"""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field

from app.core.config import get_config


def validate_model_timeout(value: int) -> int:
    """按当前部署配置校验单次模型或供应商请求超时。"""
    maximum = get_config().AGENT_MAX_MODEL_TIMEOUT_SECONDS
    if value > maximum:
        raise ValueError(f"单次模型或供应商请求超时不能超过 {maximum} 秒")
    return value


def validate_tool_timeout(value: int) -> int:
    """按当前部署配置校验单次工具超时。"""
    maximum = get_config().AGENT_MAX_TOOL_TIMEOUT_SECONDS
    if value > maximum:
        raise ValueError(f"单次工具执行超时不能超过 {maximum} 秒")
    return value


def validate_output_tokens(value: int) -> int:
    """按当前部署配置校验单次输出 Token 上限。"""
    maximum = get_config().AGENT_MAX_OUTPUT_TOKENS
    if value > maximum:
        raise ValueError(f"单次最大输出 Token 数不能超过 {maximum}")
    return value


ModelTimeout = Annotated[int, Field(ge=1), AfterValidator(validate_model_timeout)]
ToolTimeout = Annotated[int, Field(ge=1), AfterValidator(validate_tool_timeout)]
OutputTokens = Annotated[int, Field(ge=1), AfterValidator(validate_output_tokens)]


class RuntimeLimitsOut(BaseModel):
    """仅公开业务限额和默认参数，不包含部署凭据。"""

    upload_max_bytes: int = Field(description="对话上传单文件字节上限", examples=[20971520])
    context_max_bytes: int = Field(description="本轮附件正文及元数据合计 UTF-8 字节上限", examples=[20971520])
    max_files: int = Field(description="本轮附件数量上限", examples=[5])
    recursion_limit: int = Field(description="每次运行或恢复的最大图步数，非时间限制", examples=[9999])
    mcp_connect_timeout_seconds: int = Field(description="MCP HTTP 建连、写入及连接池等待时限", examples=[10])
    mcp_discovery_timeout_seconds: int = Field(description="单次 MCP 工具发现时限", examples=[15])
    mcp_call_timeout_seconds: int = Field(description="单次正式 MCP 调用时限", examples=[120])
    knowledge_search_timeout_seconds: int = Field(description="单次知识库检索时限", examples=[60])
    knowledge_embedding_timeout_seconds: int = Field(description="单批嵌入请求时限", examples=[60])
    script_execution_timeout_seconds: int = Field(description="单次正式脚本执行时限", examples=[30])
    script_test_timeout_seconds: int = Field(description="脚本试运行时限", examples=[10])
    default_model_timeout_seconds: int = Field(description="新建 Agent 默认单次模型超时秒数", examples=[120])
    max_model_timeout_seconds: int = Field(description="单次模型或供应商请求超时秒数上限", examples=[3600])
    default_tool_timeout_seconds: int = Field(description="新建 Agent 默认单次工具超时秒数", examples=[120])
    max_tool_timeout_seconds: int = Field(description="单次工具超时秒数上限", examples=[3600])
    default_max_output_tokens: int = Field(description="新建 Agent 默认单次最大输出 Token 数", examples=[4096])
    max_output_tokens: int = Field(description="允许设置的单次最大输出 Token 数", examples=[65536])
