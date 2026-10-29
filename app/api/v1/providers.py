# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:52
# @Author  : yangyuexiong
# @File    : providers.py

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import require_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import DiscoveredModelOut, ModelDiscoveryOut, ProviderCreateIn, ProviderKindOut, ProviderOut, ProviderUpdateIn
from app.services.catalog import commit_catalog, flush_catalog, get_provider_or_404, set_provider_credentials
from app.services.identity import audit
from app.services.provider_probe import normalize_base_url, probe_provider


router = APIRouter(prefix="/providers", tags=["providers"])

PROVIDER_KINDS = [
    ProviderKindOut(
        kind="openai_compatible",
        display_name="OpenAI 兼容接口",
        required_fields=["name", "kind", "base_url"],
        optional_fields=["api_key", "custom_headers", "timeout_seconds", "remark", "status"],
    ),
    ProviderKindOut(
        kind="openai",
        display_name="OpenAI",
        required_fields=["name", "kind", "base_url", "api_key"],
        optional_fields=["custom_headers", "timeout_seconds", "remark", "status"],
        suggested_base_url="https://api.openai.com/v1",
    ),
    ProviderKindOut(
        kind="anthropic",
        display_name="Anthropic",
        required_fields=["name", "kind", "base_url", "api_key"],
        optional_fields=["api_version", "custom_headers", "timeout_seconds", "remark", "status"],
        suggested_base_url="https://api.anthropic.com/v1",
    ),
    ProviderKindOut(
        kind="ollama",
        display_name="Ollama",
        required_fields=["name", "kind", "base_url"],
        optional_fields=["api_key", "custom_headers", "timeout_seconds", "remark", "status"],
        suggested_base_url="http://127.0.0.1:11434/v1",
    ),
    ProviderKindOut(
        kind="azure_openai",
        display_name="Azure OpenAI",
        required_fields=["name", "kind", "base_url", "api_key", "api_version"],
        optional_fields=["custom_headers", "timeout_seconds", "remark", "status"],
    ),
    ProviderKindOut(
        kind="gemini",
        display_name="Gemini Developer API",
        required_fields=["name", "kind", "api_key"],
        optional_fields=["base_url", "api_version", "custom_headers", "timeout_seconds", "remark", "status"],
        suggested_base_url="https://generativelanguage.googleapis.com",
    ),
    ProviderKindOut(
        kind="bedrock",
        display_name="AWS Bedrock",
        required_fields=["name", "kind", "region"],
        optional_fields=["timeout_seconds", "remark", "status"],
    ),
]


@router.get("/kinds", summary="查询支持的模型供应商类型")
async def list_provider_kinds(actor: Admin = Depends(require_admin)):
    """返回各类型适用的连接字段及建议地址。"""
    return api_response(data=[provider_kind.model_dump(mode="json") for provider_kind in PROVIDER_KINDS], is_pop=False)


@router.post("", summary="创建模型供应商连接")
async def create_provider(body: ProviderCreateIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """由管理员创建供应商连接，凭据加密后保存。"""
    name = body.name.strip()
    if body.kind == "bedrock" and (body.api_key is not None or body.custom_headers):
        raise CustomException(status_code=400, custom_code=10005, detail="Bedrock 使用本机 AWS 凭据链，不接受接口密钥或自定义请求头")
    if body.kind == "azure_openai" and not body.api_version:
        raise CustomException(status_code=400, custom_code=10005, detail="Azure OpenAI 需要 API 版本")
    if body.kind == "gemini" and body.api_version not in {None, "v1", "v1beta"}:
        raise CustomException(status_code=400, custom_code=10005, detail="Gemini 接口版本只支持 v1 或 v1beta")
    provider = ProviderConnection(
        name=name,
        name_key=name.lower(),
        kind=body.kind,
        base_url=normalize_base_url(body.base_url, kind=body.kind, region=body.region),
        api_version=body.api_version,
        region=body.region,
        timeout_seconds=body.timeout_seconds,
        remark=body.remark,
        status=body.status,
        is_deleted=0,
        creator_id=actor.id,
    )
    set_provider_credentials(provider, body)
    if body.kind in {"azure_openai", "gemini"} and not provider.has_api_key:
        raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
    db.add(provider)
    await flush_catalog(db)
    audit(db, "provider.created", actor_id=actor.id, target_id=provider.id)
    await commit_catalog(db)
    await db.refresh(provider)
    return api_response(http_code=201, code=201, data=ProviderOut.model_validate(provider).model_dump(mode="json"), datetime_format=False)


@router.get("", summary="查询模型供应商连接列表")
async def list_providers(actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """返回未删除的供应商连接，不暴露密钥或自定义请求头。"""
    providers = (
        await db.scalars(select(ProviderConnection).where(ProviderConnection.is_deleted == 0).order_by(ProviderConnection.id))
    ).all()
    data = [ProviderOut.model_validate(provider).model_dump(mode="json") for provider in providers]
    return api_response(data=data, is_pop=False)


@router.get("/{provider_id}", summary="查询模型供应商连接详情")
async def get_provider(provider_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """查询指定供应商连接的非敏感信息。"""
    provider = await get_provider_or_404(db, provider_id)
    return api_response(data=ProviderOut.model_validate(provider).model_dump(mode="json"), datetime_format=False)


@router.put("/{provider_id}", summary="修改模型供应商连接")
async def update_provider(
    provider_id: int, body: ProviderUpdateIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    """更新连接配置，未传入的凭据保持原值。"""
    provider = await get_provider_or_404(db, provider_id)
    fields = body.model_fields_set
    if "status" in fields and body.status == 0 or fields & {"base_url", "api_key", "custom_headers", "api_version", "region"}:
        await db.scalars(
            select(ModelCatalog.id)
            .where(ModelCatalog.provider_id == provider.id, ModelCatalog.is_deleted == 0)
            .order_by(ModelCatalog.id)
            .with_for_update()
        )
        linked = await db.scalar(
            select(AgentConfig.id)
            .join(ModelCatalog, AgentConfig.default_model_id == ModelCatalog.id)
            .where(ModelCatalog.provider_id == provider.id, AgentConfig.is_deleted == 0)
            .limit(1)
        )
        if linked is not None:
            raise CustomException(status_code=409, custom_code=10005, detail="连接仍被 Agent 引用，请先更换其默认模型")
    for name in ("name", "api_version", "region", "timeout_seconds", "remark", "status"):
        if name in fields:
            value = getattr(body, name)
            if name in {"name", "timeout_seconds", "status"} and value is None:
                raise CustomException(status_code=400, custom_code=10005, detail="必填字段不能为空")
            if name == "name":
                value = value.strip()
                provider.name_key = value.lower()
            setattr(provider, name, value)
    if "base_url" in fields:
        if body.base_url is None and provider.kind not in {"gemini", "bedrock"}:
            raise CustomException(status_code=400, custom_code=10005, detail="接口地址不能为空")
        provider.base_url = normalize_base_url(body.base_url, kind=provider.kind, region=provider.region)
    if provider.kind == "bedrock" and "region" in fields:
        provider.base_url = normalize_base_url(None, kind="bedrock", region=provider.region)
    if provider.kind == "azure_openai" and not provider.api_version:
        raise CustomException(status_code=400, custom_code=10005, detail="Azure OpenAI 需要 API 版本")
    if provider.kind == "gemini" and provider.api_version not in {None, "v1", "v1beta"}:
        raise CustomException(status_code=400, custom_code=10005, detail="Gemini 接口版本只支持 v1 或 v1beta")
    if provider.kind == "bedrock" and ((body.api_key is not None and "api_key" in fields) or body.custom_headers):
        raise CustomException(status_code=400, custom_code=10005, detail="Bedrock 使用本机 AWS 凭据链，不接受接口密钥或自定义请求头")
    set_provider_credentials(provider, body)
    if provider.kind in {"azure_openai", "gemini"} and not provider.has_api_key:
        raise CustomException(status_code=400, custom_code=10005, detail="该供应商需要 API 密钥")
    await flush_catalog(db)
    if fields & {"base_url", "api_key", "custom_headers", "api_version", "region"}:
        await db.execute(
            update(ModelCatalog)
            .where(ModelCatalog.provider_id == provider.id)
            .values(verified_capabilities=[], capabilities_verified=False)
        )
    if provider.status == 0:
        await db.execute(update(ModelCatalog).where(ModelCatalog.provider_id == provider.id).values(is_default=False, default_slot=None))
    audit(db, "provider.updated", actor_id=actor.id, target_id=provider.id)
    await commit_catalog(db)
    await db.refresh(provider)
    return api_response(data=ProviderOut.model_validate(provider).model_dump(mode="json"), datetime_format=False)


@router.delete("/{provider_id}", summary="删除模型供应商连接")
async def delete_provider(provider_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """仅删除尚未关联有效模型的供应商连接。"""
    provider = await get_provider_or_404(db, provider_id)
    linked = await db.scalar(select(ModelCatalog.id).where(ModelCatalog.provider_id == provider.id, ModelCatalog.is_deleted == 0).limit(1))
    if linked is not None:
        raise CustomException(status_code=409, custom_code=10005, detail="该连接仍关联模型，不能删除")
    provider.is_deleted = 1
    provider.status = 0
    audit(db, "provider.deleted", actor_id=actor.id, target_id=provider.id)
    await db.commit()
    return api_response(message="删除成功")


@router.post("/{provider_id}/test", summary="测试模型供应商连接")
async def test_provider(provider_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """请求供应商模型列表并返回模型 ID、耗时及故障类型。"""
    provider = await get_provider_or_404(db, provider_id)
    result = await probe_provider(provider)
    audit(db, "provider.tested", actor_id=actor.id, target_id=provider.id)
    await db.commit()
    return api_response(data=result.model_dump())


@router.get("/{provider_id}/available-models", summary="发现供应商可用模型")
async def discover_provider_models(provider_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """读取供应商模型列表，并标记目录中已录入的模型。"""
    provider = await get_provider_or_404(db, provider_id)
    probe = await probe_provider(provider)
    existing_ids: set[str] = set()
    if probe.models:
        existing_ids = set(
            await db.scalars(
                select(ModelCatalog.external_id).where(
                    ModelCatalog.provider_id == provider_id, ModelCatalog.external_id.in_(probe.models), ModelCatalog.is_deleted == 0
                )
            )
        )
    result = ModelDiscoveryOut(
        available=probe.available,
        latency_ms=probe.latency_ms,
        upstream_status_code=probe.upstream_status_code,
        model_count=probe.model_count,
        failure=probe.failure,
        models=[
            DiscoveredModelOut(
                external_id=model_id, display_name=probe.model_names.get(model_id, model_id)[:128], already_added=model_id in existing_ids
            )
            for model_id in probe.models
        ],
    )
    return api_response(data=result.model_dump())
