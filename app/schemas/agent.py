# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 11:00:31
# @Author  : yangyuexiong
# @File    : agent.py

"""私有 Agent 配置接口结构。"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class AgentCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: AgentName = Field(description="智能体名称", examples=["需求分析助手"])
    description: str | None = Field(default=None, max_length=1024, description="智能体描述", examples=["分析业务需求"])
    icon: str | None = Field(default=None, max_length=128, description="图标标识", examples=["bot"])
    system_prompt: str = Field(default="", max_length=32768, description="系统指令", examples=["请使用简洁语言回答"])
    welcome_message: str | None = Field(default=None, max_length=2048, description="欢迎语", examples=["你好，请描述需求"])
    default_model_id: int = Field(gt=0, description="默认模型 ID", examples=[1])
    temperature: Decimal = Field(default=Decimal("1"), ge=0, le=2, decimal_places=2, description="生成温度", examples=["0.70"])
    max_output_tokens: int = Field(default=4096, ge=1, le=65536, description="单次最大输出 Token 数", examples=[4096])
    model_timeout_seconds: int = Field(default=120, ge=1, le=120, description="单次模型生成超时秒数", examples=[60])
    tool_timeout_seconds: int = Field(default=120, ge=1, le=120, description="单次工具执行超时秒数", examples=[30])
    max_model_calls: int | None = Field(default=None, ge=1, le=100, description="每轮模型调用次数上限", examples=[3])
    max_tool_calls: int | None = Field(default=None, ge=1, le=100, description="每轮工具调用次数上限", examples=[5])
    max_total_tokens: int | None = Field(default=None, ge=1, le=10000000, description="每轮上游报告的总 Token 上限", examples=[10000])
    max_estimated_cost: Decimal | None = Field(
        default=None, ge=Decimal("0.000001"), max_digits=18, decimal_places=6,
        description="每轮按模型价格估算的成本上限", examples=["0.010000"],
    )
    enabled: bool = Field(default=True, description="是否启用", examples=[True])


class AgentUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="提交修改时读取到的配置版本号", examples=[1])
    name: AgentName | None = Field(default=None, description="智能体名称", examples=["需求分析助手"])
    description: str | None = Field(default=None, max_length=1024, description="智能体描述", examples=["分析业务需求"])
    icon: str | None = Field(default=None, max_length=128, description="图标标识", examples=["bot"])
    system_prompt: str | None = Field(default=None, max_length=32768, description="系统指令", examples=["请使用简洁语言回答"])
    welcome_message: str | None = Field(default=None, max_length=2048, description="欢迎语", examples=["你好，请描述需求"])
    default_model_id: int | None = Field(default=None, gt=0, description="默认模型 ID", examples=[1])
    temperature: Decimal | None = Field(default=None, ge=0, le=2, decimal_places=2, description="生成温度", examples=["0.70"])
    max_output_tokens: int | None = Field(default=None, ge=1, le=65536, description="单次最大输出 Token 数", examples=[4096])
    model_timeout_seconds: int | None = Field(default=None, ge=1, le=120, description="单次模型生成超时秒数", examples=[60])
    tool_timeout_seconds: int | None = Field(default=None, ge=1, le=120, description="单次工具执行超时秒数", examples=[30])
    max_model_calls: int | None = Field(default=None, ge=1, le=100, description="每轮模型调用次数上限；null 清除上限", examples=[3])
    max_tool_calls: int | None = Field(default=None, ge=1, le=100, description="每轮工具调用次数上限；null 清除上限", examples=[5])
    max_total_tokens: int | None = Field(
        default=None, ge=1, le=10000000, description="每轮上游报告的总 Token 上限；null 清除上限", examples=[10000],
    )
    max_estimated_cost: Decimal | None = Field(
        default=None, ge=Decimal("0.000001"), max_digits=18, decimal_places=6,
        description="每轮估算成本上限；null 清除上限", examples=["0.010000"],
    )
    enabled: bool | None = Field(default=None, description="是否启用", examples=[False])


class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="智能体 ID", examples=[1])
    name: str = Field(description="智能体名称", examples=["需求分析助手"])
    description: str | None = Field(description="智能体描述", examples=["分析业务需求"])
    icon: str | None = Field(description="图标标识", examples=["bot"])
    system_prompt: str = Field(description="系统指令", examples=["请使用简洁语言回答"])
    welcome_message: str | None = Field(description="欢迎语", examples=["你好，请描述需求"])
    default_model_id: int = Field(description="默认模型 ID", examples=[1])
    temperature: Decimal = Field(description="生成温度", examples=["0.70"])
    max_output_tokens: int = Field(description="单次最大输出 Token 数", examples=[4096])
    model_timeout_seconds: int = Field(description="单次模型生成超时秒数", examples=[60])
    tool_timeout_seconds: int = Field(description="单次工具执行超时秒数", examples=[30])
    max_model_calls: int | None = Field(description="每轮模型调用次数上限", examples=[3])
    max_tool_calls: int | None = Field(description="每轮工具调用次数上限", examples=[5])
    max_total_tokens: int | None = Field(description="每轮上游报告的总 Token 上限", examples=[10000])
    max_estimated_cost: Decimal | None = Field(description="每轮估算成本上限", examples=["0.010000"])
    enabled: bool = Field(description="是否启用", examples=[True])
    version: int = Field(description="配置版本号", examples=[1])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-26T11:00:00"])
    update_time: datetime = Field(description="更新时间", examples=["2026-10-26T11:00:00"])


class AgentPageOut(BaseModel):
    items: list[AgentOut] = Field(description="当前页的智能体配置", examples=[[]])
    total: int = Field(description="符合条件的总数", examples=[1])
    page: int = Field(description="页码", examples=[1])
    page_size: int = Field(description="每页数量", examples=[20])


class AgentShareIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0, description="指定共享的用户 ID", examples=[2])


class AgentShareOut(BaseModel):
    user_id: int = Field(description="获授权用户 ID", examples=[2])
    username: str = Field(description="获授权用户名", examples=["alice"])


class SharedAgentOut(BaseModel):
    id: int = Field(description="智能体 ID", examples=[1])
    owner_id: int = Field(description="智能体所有者 ID", examples=[1])
    name: str = Field(description="智能体名称", examples=["需求分析助手"])
    description: str | None = Field(description="智能体描述")
    icon: str | None = Field(description="图标标识")
    welcome_message: str | None = Field(description="欢迎语")
    enabled: bool = Field(description="是否启用")
