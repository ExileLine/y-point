# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:21:38
# @Author  : yangyuexiong
# @File    : run.py

"""公开文本运行的请求和响应结构。"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: int = Field(gt=0, description="本次运行使用的可选模型 ID", examples=[1])
    model_timeout_seconds: int = Field(default=120, ge=1, le=120, description="本轮模型生成最长秒数", examples=[60])
    tool_timeout_seconds: int = Field(default=120, ge=1, le=120, description="单次工具执行最长秒数", examples=[30])
    max_model_calls: int | None = Field(
        default=None, ge=1, le=100, description="本次运行最多可开始的模型调用次数", examples=[3],
    )
    max_total_tokens: int | None = Field(
        default=None, ge=1, le=10000000, description="本次运行上游报告的总 Token 上限", examples=[10000],
    )
    max_estimated_cost: Decimal | None = Field(
        default=None, ge=Decimal("0.000001"), max_digits=18, decimal_places=6,
        description="本次运行按模型配置单价估算的成本上限，币种由模型决定", examples=["0.010000"],
    )
    max_tool_calls: int | None = Field(
        default=None, ge=1, le=100, description="本次运行最多可开始的工具调用次数", examples=[5],
    )
    input_text: str = Field(min_length=1, max_length=32768, description="用户输入的文本", examples=["请介绍一下你自己"])
    request_key: str | None = Field(
        default=None, min_length=1, max_length=128, description="重复提交时复用运行的幂等键", examples=["chat-001"]
    )
    thread_id: str | None = Field(
        default=None, min_length=1, max_length=36, description="续接已有运行的检查点线程 ID",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )


class RunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="运行 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    model_id: int | None = Field(description="使用的模型 ID", examples=[1])
    model_timeout_seconds: int = Field(description="本轮模型生成最长秒数", examples=[60])
    tool_timeout_seconds: int = Field(description="单次工具执行最长秒数", examples=[30])
    max_model_calls: int | None = Field(description="本次运行的模型调用次数上限", examples=[3])
    model_call_count: int = Field(description="本次运行已开始的模型调用次数", examples=[1])
    max_total_tokens: int | None = Field(description="本次运行上游报告的总 Token 上限", examples=[10000])
    reported_total_tokens: int = Field(description="本次运行上游已报告的总 Token 数", examples=[1250])
    max_estimated_cost: Decimal | None = Field(description="本次运行估算成本上限", examples=["0.010000"])
    estimated_cost: Decimal = Field(description="启用成本预算时累计的估算成本", examples=["0.000250000000"])
    cost_currency: str | None = Field(description="成本预算的价格币种", examples=["USD"])
    max_tool_calls: int | None = Field(description="本次运行的工具调用次数上限", examples=[5])
    tool_call_count: int = Field(description="本次运行已开始的工具调用次数", examples=[2])
    thread_id: str = Field(description="检查点线程 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    state: str = Field(description="运行状态", examples=["queued"])
    event_seq: int = Field(description="最新事件序号", examples=[1])
    failure_code: str | None = Field(description="脱敏故障类别", examples=["model_error"])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-22T22:00:00"])
    update_time: datetime = Field(description="更新时间", examples=["2026-10-22T22:00:00"])


class RunEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sequence: int = Field(description="运行内单调事件序号", examples=[2])
    protocol_version: int = Field(description="事件协议版本", examples=[1])
    event_type: str = Field(description="事件类型", examples=["state"])
    state: str = Field(description="事件发生后的运行状态", examples=["running"])
    failure_code: str | None = Field(description="脱敏故障类别", examples=["model_error"])
    content: str | None = Field(description="可公开的模型文本增量", examples=["你好"])
    tool_call_id: str | None = Field(
        description="服务端生成的工具调用关联 ID", examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    tool_name: str | None = Field(description="已授权工具名称，不含参数和结果", examples=["web_search"])
    create_time: datetime = Field(description="事件创建时间", examples=["2026-10-22T22:00:00"])


class RunMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    role: Literal["user", "assistant"] = Field(description="消息角色", examples=["assistant"])
    content: str = Field(description="用户输入或最终可见回答", examples=["你好，我可以帮助你。"])
    create_time: datetime = Field(description="消息创建时间", examples=["2026-10-22T22:00:00"])


class ApprovalDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"] = Field(description="审批决定：同意或拒绝", examples=["approve"])


class ApprovalActionOut(BaseModel):
    name: str = Field(description="待审批工具名称", examples=["web_search"])
    arguments_hash: str = Field(description="工具参数的服务端 HMAC-SHA256 摘要", examples=["a" * 64])


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="审批请求 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    run_id: str = Field(description="所属运行 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    actions: list[ApprovalActionOut] = Field(
        description="待审批的工具及参数摘要", examples=[[{"name": "web_search", "arguments_hash": "a" * 64}]]
    )
    state: str = Field(description="审批状态", examples=["pending"])
    expires_at: datetime = Field(description="审批到期时间", examples=["2026-10-22T23:00:00"])
    decided_at: datetime | None = Field(description="审批决定时间", examples=["2026-10-22T22:30:00"])


class WorkerStatusOut(BaseModel):
    worker_available: bool = Field(description="是否有心跳未过期的执行器", examples=[True])
    active_workers: int = Field(ge=0, description="心跳未过期的执行器数量", examples=[1])
    latest_heartbeat_at: datetime | None = Field(description="最近一次执行器心跳时间", examples=["2026-10-23T10:00:00"])
    queued_runs: int = Field(ge=0, description="等待执行的运行数量", examples=[2])
    oldest_queued_seconds: int | None = Field(description="最早排队运行已等待的秒数", examples=[35])
    expired_running_runs: int = Field(ge=0, description="租约已过期但尚未回收的运行数量", examples=[0])
