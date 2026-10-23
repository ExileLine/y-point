# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:07
# @Author  : yangyuexiong
# @File    : provider.py

from decimal import Decimal
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints


ProviderKind = Literal["openai_compatible", "openai", "anthropic", "ollama", "azure_openai", "gemini", "bedrock"]
Capability = Literal["text", "vision", "tools", "streaming", "structured_output", "reasoning_summary"]
ProbeCapability = Literal["text", "streaming", "tools", "structured_output", "vision", "reasoning_summary"]
ConnectionName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
BaseUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
ExternalModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class ProviderCreateIn(BaseModel):
    name: ConnectionName = Field(description="供应商连接名称", examples=["生产环境 OpenAI"])
    kind: ProviderKind = Field(description="供应商类型", examples=["openai_compatible"])
    base_url: BaseUrl | None = Field(default=None, description="供应商接口基础地址；Gemini 和 Bedrock 可省略", examples=["https://api.example.com/v1"])
    api_key: SecretStr | None = Field(default=None, description="接口密钥", examples=["sk-example-key"])
    custom_headers: dict[str, SecretStr] | None = Field(default=None, description="自定义请求头", examples=[{"X-Tenant": "example-tenant"}])
    api_version: str | None = Field(default=None, max_length=64, description="接口版本", examples=["2024-01-01"])
    region: str | None = Field(default=None, max_length=64, description="服务区域", examples=["us-east-1"])
    timeout_seconds: int = Field(default=15, ge=1, le=120, description="请求超时时间（秒）", examples=[15])
    remark: str | None = Field(default=None, max_length=512, description="备注", examples=["用于生产环境"])
    status: Literal[0, 1] = Field(default=1, description="状态：1 启用，0 停用", examples=[1])


class ProviderUpdateIn(BaseModel):
    name: ConnectionName | None = Field(default=None, description="供应商连接名称", examples=["备用连接"])
    base_url: BaseUrl | None = Field(default=None, description="供应商接口基础地址", examples=["https://api.example.com/v1"])
    api_key: SecretStr | None = Field(
        default=None, description="接口密钥；省略时保留原值，传入 null 时清除", examples=["sk-new-example-key"]
    )
    custom_headers: dict[str, SecretStr] | None = Field(
        default=None, description="自定义请求头；省略时保留原值，传入 null 时清除", examples=[{"X-Tenant": "new-example-tenant"}]
    )
    api_version: str | None = Field(default=None, max_length=64, description="接口版本", examples=["2024-01-01"])
    region: str | None = Field(default=None, max_length=64, description="服务区域", examples=["us-east-1"])
    timeout_seconds: int | None = Field(default=None, ge=1, le=120, description="请求超时时间（秒）", examples=[30])
    remark: str | None = Field(default=None, max_length=512, description="备注", examples=["已验证连接"])
    status: Literal[0, 1] | None = Field(default=None, description="状态：1 启用，0 停用", examples=[0])


class ProviderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="供应商连接 ID", examples=[1])
    name: str = Field(description="供应商连接名称", examples=["生产环境 OpenAI"])
    kind: ProviderKind = Field(description="供应商类型", examples=["openai_compatible"])
    base_url: str = Field(description="供应商接口基础地址", examples=["https://api.example.com/v1"])
    has_api_key: bool = Field(description="是否已配置接口密钥", examples=[True])
    has_custom_headers: bool = Field(description="是否已配置自定义请求头", examples=[False])
    api_version: str | None = Field(description="接口版本", examples=["2024-01-01"])
    region: str | None = Field(description="服务区域", examples=["us-east-1"])
    timeout_seconds: int = Field(description="请求超时时间（秒）", examples=[15])
    remark: str | None = Field(description="备注", examples=["用于生产环境"])
    status: int | None = Field(description="状态：1 启用，0 停用", examples=[1])
    creator_id: int = Field(description="创建人 ID", examples=[1])


class ProviderKindOut(BaseModel):
    kind: ProviderKind = Field(description="供应商类型标识", examples=["ollama"])
    display_name: str = Field(description="供应商类型名称", examples=["Ollama"])
    required_fields: list[str] = Field(description="创建连接时需要展示的必填字段", examples=[["name", "kind", "base_url"]])
    optional_fields: list[str] = Field(description="创建连接时可展示的选填字段", examples=[["timeout_seconds", "remark"]])
    suggested_base_url: str | None = Field(default=None, description="建议的接口基础地址", examples=["http://127.0.0.1:11434/v1"])


class ModelCreateIn(BaseModel):
    provider_id: int = Field(gt=0, description="所属供应商连接 ID", examples=[1])
    external_id: ExternalModelId = Field(description="供应商侧模型 ID", examples=["gpt-4o-mini"])
    display_name: DisplayName = Field(description="模型显示名称", examples=["GPT-4o mini"])
    capabilities: list[Capability] = Field(
        default_factory=list, description="模型支持的能力，留空表示待配置", examples=[["text", "streaming"]]
    )
    context_window: int | None = Field(default=None, gt=0, description="上下文窗口大小（令牌数）", examples=[128000])
    input_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6, description="输入价格", examples=["0.000150"])
    output_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6, description="输出价格", examples=["0.000600"])
    price_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$", description="价格币种（三位大写代码）", examples=["USD"])
    status: Literal[0, 1] = Field(default=1, description="状态：1 启用，0 停用", examples=[1])


class ModelUpdateIn(BaseModel):
    external_id: ExternalModelId | None = Field(default=None, description="供应商侧模型 ID", examples=["gpt-4o-mini"])
    display_name: DisplayName | None = Field(default=None, description="模型显示名称", examples=["GPT-4o mini"])
    capabilities: list[Capability] | None = Field(
        default=None, description="模型支持的能力，空列表表示待配置", examples=[["text", "tools"]]
    )
    context_window: int | None = Field(default=None, gt=0, description="上下文窗口大小（令牌数）", examples=[128000])
    input_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6, description="输入价格", examples=["0.000150"])
    output_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6, description="输出价格", examples=["0.000600"])
    price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$", description="价格币种（三位大写代码）", examples=["USD"])
    status: Literal[0, 1] | None = Field(default=None, description="状态：1 启用，0 停用", examples=[0])


class ModelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="模型 ID", examples=[1])
    provider_id: int = Field(description="所属供应商连接 ID", examples=[1])
    external_id: str = Field(description="供应商侧模型 ID", examples=["gpt-4o-mini"])
    display_name: str = Field(description="模型显示名称", examples=["GPT-4o mini"])
    capabilities: list[Capability] = Field(description="模型支持的能力", examples=[["text", "streaming"]])
    verified_capabilities: list[Capability] = Field(description="实际探测通过的模型能力", examples=[["text"]])
    capabilities_verified: bool = Field(description="能力是否已验证", examples=[False])
    context_window: int | None = Field(description="上下文窗口大小（令牌数）", examples=[128000])
    input_price: Decimal | None = Field(description="输入价格", examples=["0.000150"])
    output_price: Decimal | None = Field(description="输出价格", examples=["0.000600"])
    price_currency: str = Field(description="价格币种（三位大写代码）", examples=["USD"])
    is_default: bool = Field(description="是否为默认模型", examples=[True])
    status: int | None = Field(description="状态：1 启用，0 停用", examples=[1])
    creator_id: int = Field(description="创建人 ID", examples=[1])


class ModelImportItem(BaseModel):
    external_id: ExternalModelId = Field(description="从供应商列表选中的模型 ID", examples=["gpt-4o-mini"])
    display_name: DisplayName | None = Field(default=None, description="模型显示名称；省略时使用模型 ID", examples=["GPT-4o mini"])
    capabilities: list[Capability] = Field(default_factory=list, description="模型支持的能力，留空表示待配置", examples=[["text"]])
    context_window: int | None = Field(default=None, gt=0, description="上下文窗口大小（令牌数）", examples=[128000])
    input_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6, description="输入价格", examples=["0.000150"])
    output_price: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=6, description="输出价格", examples=["0.000600"])
    price_currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$", description="价格币种（三位大写代码）", examples=["USD"])
    status: Literal[0, 1] = Field(default=1, description="状态：1 启用，0 停用", examples=[1])


class ModelImportIn(BaseModel):
    provider_id: int = Field(gt=0, description="所属供应商连接 ID", examples=[1])
    models: list[ModelImportItem] = Field(
        min_length=1, max_length=100, description="选中后要批量导入的模型及补充参数", examples=[[{"external_id": "gpt-4o-mini"}]]
    )


class ModelImportOut(BaseModel):
    created: list[ModelOut] = Field(description="本次新增或恢复的模型", examples=[[]])
    skipped_existing: list[str] = Field(description="已存在并跳过的模型 ID", examples=[["gpt-4o-mini"]])


class DiscoveredModelOut(BaseModel):
    external_id: str = Field(description="供应商返回的模型 ID", examples=["gpt-4o-mini"])
    display_name: str = Field(description="建议的模型显示名称，可在导入时修改", examples=["gpt-4o-mini"])
    already_added: bool = Field(description="该模型是否已录入目录", examples=[False])


class ModelDiscoveryOut(BaseModel):
    available: bool = Field(description="模型列表接口是否可用", examples=[True])
    latency_ms: int = Field(description="模型列表请求耗时（毫秒）", examples=[120])
    upstream_status_code: int | None = Field(default=None, description="上游 HTTP 状态码", examples=[200])
    model_count: int | None = Field(default=None, description="供应商返回的模型数量", examples=[2])
    models: list[DiscoveredModelOut] = Field(default_factory=list, description="可选择的模型列表", examples=[[]])
    failure: Literal[
        "authentication", "rate_limit", "model_list_unavailable", "timeout", "network", "upstream", "invalid_response"
    ] | None = Field(
        default=None, description="模型列表获取失败的原因", examples=["timeout"]
    )


class ConnectionTestOut(BaseModel):
    available: bool = Field(description="连接是否可用", examples=[True])
    latency_ms: int = Field(description="连接测试耗时（毫秒）", examples=[120])
    upstream_status_code: int | None = Field(default=None, description="上游 HTTP 状态码", examples=[200])
    model_count: int | None = Field(default=None, description="上游返回的模型数量", examples=[3])
    models: list[str] = Field(default_factory=list, description="上游返回的可用模型 ID 列表", examples=[["gpt-4o-mini", "gpt-4o"]])
    model_names: dict[str, str] = Field(
        default_factory=dict, description="上游提供的模型显示名称", examples=[{"gpt-4o-mini": "GPT-4o mini"}]
    )
    failure: Literal[
        "authentication", "rate_limit", "model_list_unavailable", "timeout", "network", "upstream", "invalid_response"
    ] | None = Field(
        default=None, description="连接失败原因", examples=["timeout"]
    )


class ModelTestOut(BaseModel):
    record_id: int | None = Field(default=None, description="已保存的调用记录 ID", examples=[1])
    model_id: int = Field(description="被测试的模型 ID", examples=[1])
    external_id: str = Field(description="供应商侧模型 ID", examples=["gpt-4o-mini"])
    available: bool = Field(description="本次模型探测调用是否成功", examples=[True])
    latency_ms: int = Field(description="模型调用耗时（毫秒）", examples=[350])
    upstream_status_code: int | None = Field(default=None, description="上游 HTTP 状态码", examples=[200])
    input_tokens: int | None = Field(default=None, description="上游报告的输入 Token 数", examples=[24])
    output_tokens: int | None = Field(default=None, description="上游报告的输出 Token 数", examples=[8])
    estimated_cost: Decimal | None = Field(default=None, description="依据模型单价估算的调用成本", examples=["0.000004"])
    price_currency: str | None = Field(default=None, description="估算成本的币种", examples=["USD"])
    failure: Literal[
        "authentication", "model_not_found", "invalid_request", "rate_limit", "timeout", "network", "upstream", "invalid_response",
        "unsupported_capability",
    ] | None = Field(default=None, description="测试失败原因", examples=["rate_limit"])


class CapabilityTestIn(BaseModel):
    capability: ProbeCapability = Field(description="本次要验证的能力", examples=["streaming"])


class CapabilityTestOut(BaseModel):
    capability: ProbeCapability = Field(description="本次验证的能力", examples=["streaming"])
    result: ModelTestOut = Field(
        description="脱敏后的调用结果",
        examples=[{"model_id": 1, "external_id": "gpt-4o-mini", "available": True, "latency_ms": 300}],
    )
    verified_capabilities: list[Capability] = Field(description="当前已通过探测的能力", examples=[["text", "streaming"]])


class ModelCallOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="调用记录 ID", examples=[1])
    actor_id: int = Field(description="发起调用的用户 ID", examples=[1])
    provider_id: int = Field(description="调用时的供应商连接 ID", examples=[1])
    model_id: int = Field(description="调用时的模型 ID", examples=[1])
    run_id: str | None = Field(description="关联运行 ID；管理员探测时为空", examples=["550e8400-e29b-41d4-a716-446655440000"])
    external_id: str = Field(description="调用时的供应商模型 ID", examples=["gpt-4o-mini"])
    call_type: str = Field(description="调用类型", examples=["text_probe"])
    available: bool = Field(description="调用是否成功", examples=[True])
    latency_ms: int = Field(description="调用耗时（毫秒）", examples=[350])
    upstream_status_code: int | None = Field(description="上游 HTTP 状态码", examples=[200])
    failure: str | None = Field(description="脱敏后的故障类别", examples=["rate_limit"])
    input_tokens: int | None = Field(description="上游报告的输入 Token 数", examples=[24])
    output_tokens: int | None = Field(description="上游报告的输出 Token 数", examples=[8])
    estimated_cost: Decimal | None = Field(description="依据模型单价估算的调用成本", examples=["0.000004"])
    price_currency: str | None = Field(description="估算成本的币种", examples=["USD"])
    create_time: datetime = Field(description="调用记录创建时间", examples=["2026-10-22T14:43:17"])


class ModelCallPageOut(BaseModel):
    records: list[ModelCallOut] = Field(description="当前页的模型调用记录", examples=[[]])
    page: int = Field(description="当前页码", examples=[1])
    size: int = Field(description="每页数量", examples=[20])
    total: int = Field(description="符合条件的记录总数", examples=[1])
