# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:52
# @Author  : yangyuexiong
# @File    : models.py

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import get_current_admin, require_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.model_catalog import ModelCatalog
from app.models.model_call_record import ModelCallRecord
from app.models.provider_connection import ProviderConnection
from app.schemas.agent import AgentCreateIn
from app.schemas.provider import (
    CapabilityTestIn,
    CapabilityTestOut,
    AgentModelOptionOut,
    ModelCallOut,
    ModelCallPageOut,
    ModelCreateIn,
    ModelImportIn,
    ModelImportOut,
    ModelOut,
    ModelUpdateIn,
)
from app.services.catalog import commit_catalog, flush_catalog, get_model_or_404, get_provider_or_404
from app.services.capability_probe import probe_capability
from app.services.identity import audit
from app.services.model_probe import probe_model
from app.services.runtime_adapter import CONTEXT_OVERHEAD_TOKENS, MIN_CONTEXT_INPUT_TOKENS


router = APIRouter(prefix="/models", tags=["models"])


@router.post("", summary="创建可选模型")
async def create_model(body: ModelCreateIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """在指定供应商连接下登记模型及可选的能力信息。"""
    await get_provider_or_404(db, body.provider_id)
    model = ModelCatalog(
        provider_id=body.provider_id,
        external_id=body.external_id.strip(),
        display_name=body.display_name.strip(),
        capabilities=list(dict.fromkeys(body.capabilities)),
        capabilities_verified=False,
        context_window=body.context_window,
        input_price=body.input_price,
        output_price=body.output_price,
        price_currency=body.price_currency,
        is_default=False,
        status=body.status,
        is_deleted=0,
        creator_id=actor.id,
    )
    db.add(model)
    await flush_catalog(db)
    audit(db, "model.created", actor_id=actor.id, target_id=model.id)
    await commit_catalog(db)
    await db.refresh(model)
    return api_response(http_code=201, code=201, data=ModelOut.model_validate(model).model_dump(mode="json"), datetime_format=False)


@router.post("/import", summary="批量导入供应商模型")
async def import_models(body: ModelImportIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """批量新增或恢复选中的模型，重复导入时跳过现有记录。"""
    await get_provider_or_404(db, body.provider_id)
    external_ids = [item.external_id for item in body.models]
    if len(external_ids) != len(set(external_ids)):
        raise CustomException(status_code=400, custom_code=10005, detail="请求中包含重复模型 ID")
    existing = (
        await db.scalars(
            select(ModelCatalog).where(ModelCatalog.provider_id == body.provider_id, ModelCatalog.external_id.in_(external_ids))
        )
    ).all()
    existing_by_id = {model.external_id: model for model in existing}
    created: list[ModelCatalog] = []
    skipped_existing: list[str] = []
    for item in body.models:
        model = existing_by_id.get(item.external_id)
        if model is not None and model.is_deleted == 0:
            skipped_existing.append(item.external_id)
            continue
        if model is None:
            model = ModelCatalog(provider_id=body.provider_id, external_id=item.external_id, creator_id=actor.id)
            db.add(model)
        model.display_name = item.display_name or item.external_id[:128]
        model.capabilities = list(dict.fromkeys(item.capabilities))
        model.verified_capabilities = []
        model.capabilities_verified = False
        model.context_window = item.context_window
        model.input_price = item.input_price
        model.output_price = item.output_price
        model.price_currency = item.price_currency
        model.status = item.status
        model.is_default = False
        model.default_slot = None
        model.is_deleted = 0
        created.append(model)
    if created:
        await flush_catalog(db)
        for model in created:
            audit(db, "model.imported", actor_id=actor.id, target_id=model.id)
        await commit_catalog(db)
        for model in created:
            await db.refresh(model)
    result = ModelImportOut(created=[ModelOut.model_validate(model) for model in created], skipped_existing=skipped_existing)
    http_code = 201 if created else 200
    return api_response(http_code=http_code, code=http_code, data=result.model_dump(mode="json"), datetime_format=False)


@router.get("", summary="查询可选模型列表")
async def list_models(
    provider_id: int | None = Query(default=None, gt=0), actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    """列出未删除模型，可按供应商连接筛选。"""
    query = select(ModelCatalog).where(ModelCatalog.is_deleted == 0)
    if provider_id is not None:
        query = query.where(ModelCatalog.provider_id == provider_id)
    models = (await db.scalars(query.order_by(ModelCatalog.id))).all()
    return api_response(data=[ModelOut.model_validate(model).model_dump(mode="json") for model in models], is_pop=False)


@router.get("/available", summary="查询可用于创建 Agent 的模型")
async def list_available_models(actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """向登录用户提供已验证文本能力的最小模型目录。"""
    minimum_context_window = AgentCreateIn.model_fields["max_output_tokens"].default + CONTEXT_OVERHEAD_TOKENS + MIN_CONTEXT_INPUT_TOKENS
    rows = await db.execute(
        select(ModelCatalog, ProviderConnection)
        .join(ProviderConnection, ModelCatalog.provider_id == ProviderConnection.id)
        .where(
            ModelCatalog.is_deleted == 0,
            ModelCatalog.status == 1,
            ProviderConnection.is_deleted == 0,
            ProviderConnection.status == 1,
        )
        .order_by(ModelCatalog.is_default.desc(), ModelCatalog.id)
    )
    data = [
        AgentModelOptionOut(id=model.id, display_name=model.display_name, context_window=model.context_window).model_dump(mode="json")
        for model, _provider in rows
        if "text" in (model.verified_capabilities or [])
        and (model.context_window is None or model.context_window >= minimum_context_window)
    ]
    return api_response(data=data, is_pop=False)


@router.get("/{model_id}", summary="查询可选模型详情")
async def get_model(model_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """读取指定模型的配置和能力标记。"""
    model = await get_model_or_404(db, model_id)
    return api_response(data=ModelOut.model_validate(model).model_dump(mode="json"), datetime_format=False)


@router.post("/{model_id}/test", summary="测试指定模型的文本生成调用")
async def test_model(model_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """发送一次最小文本生成请求并返回不含上游响应正文的诊断结果。"""
    model = await get_model_or_404(db, model_id)
    provider = await get_provider_or_404(db, model.provider_id)
    if model.status != 1 or provider.status != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="停用的模型或连接不能测试")
    result = await probe_model(model, provider)
    record = ModelCallRecord(
        actor_id=actor.id,
        provider_id=provider.id,
        model_id=model.id,
        external_id=model.external_id,
        call_type="text_probe",
        available=result.available,
        latency_ms=result.latency_ms,
        upstream_status_code=result.upstream_status_code,
        failure=result.failure,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost=result.estimated_cost,
        price_currency=result.price_currency,
    )
    db.add(record)
    await db.flush()
    result.record_id = record.id
    audit(db, "model.tested", actor_id=actor.id, target_id=model.id)
    await db.commit()
    return api_response(data=result.model_dump(mode="json"))


@router.post("/{model_id}/capabilities/test", summary="验证模型的指定能力")
async def test_model_capability(
    model_id: int, body: CapabilityTestIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    """执行一次指定能力的实际调用，记录安全结果并维护逐项验证状态。"""
    model = await get_model_or_404(db, model_id)
    provider = await get_provider_or_404(db, model.provider_id)
    if model.status != 1 or provider.status != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="停用的模型或连接不能测试")
    external_id = model.external_id
    provider_config = (provider.base_url, provider.encrypted_credentials, provider.api_version)
    result = await probe_capability(model, provider, body.capability)
    record = ModelCallRecord(
        actor_id=actor.id,
        provider_id=provider.id,
        model_id=model.id,
        external_id=external_id,
        call_type=f"{body.capability}_probe",
        available=result.available,
        latency_ms=result.latency_ms,
        upstream_status_code=result.upstream_status_code,
        failure=result.failure,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost=result.estimated_cost,
        price_currency=result.price_currency,
    )
    db.add(record)
    await db.flush()
    result.record_id = record.id
    await db.refresh(model)
    await db.refresh(provider)
    if (
        model.status == 1
        and provider.status == 1
        and model.is_deleted == 0
        and provider.is_deleted == 0
        and model.external_id == external_id
        and (provider.base_url, provider.encrypted_credentials, provider.api_version) == provider_config
    ):
        verified = set(model.verified_capabilities or [])
        if result.available:
            verified.add(body.capability)
            model.capabilities = list(dict.fromkeys([*(model.capabilities or []), body.capability]))
        else:
            verified.discard(body.capability)
        model.verified_capabilities = sorted(verified)
        model.capabilities_verified = bool(model.capabilities) and set(model.capabilities).issubset(verified)
    audit(db, "model.capability_tested", actor_id=actor.id, target_id=model.id)
    await db.commit()
    response = CapabilityTestOut(capability=body.capability, result=result, verified_capabilities=model.verified_capabilities or [])
    return api_response(data=response.model_dump(mode="json"))


@router.get("/{model_id}/calls", summary="查询模型调用记录")
async def list_model_calls(
    model_id: int,
    page: int = Query(default=1, ge=1, description="页码"),
    size: int = Query(default=20, ge=1, le=200, description="每页数量"),
    actor: Admin = Depends(require_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """按时间倒序查询指定模型的脱敏调用记录，保留已删除模型的历史。"""
    model = await db.get(ModelCatalog, model_id)
    if model is None:
        raise CustomException(status_code=404, custom_code=10002, detail="模型不存在")
    condition = ModelCallRecord.model_id == model_id
    total = await db.scalar(select(func.count()).select_from(ModelCallRecord).where(condition))
    records = (
        await db.scalars(select(ModelCallRecord).where(condition).order_by(ModelCallRecord.id.desc()).offset((page - 1) * size).limit(size))
    ).all()
    result = ModelCallPageOut(records=[ModelCallOut.model_validate(record) for record in records], page=page, size=size, total=total or 0)
    return api_response(data=result.model_dump(mode="json"), datetime_format=False)


@router.put("/{model_id}", summary="修改可选模型")
async def update_model(
    model_id: int, body: ModelUpdateIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    """修改模型信息；人工更改能力后重新标为未验证。"""
    await db.scalar(select(ModelCatalog.id).where(ModelCatalog.id == model_id).with_for_update())
    model = await get_model_or_404(db, model_id)
    fields = body.model_fields_set
    if ("status" in fields and body.status == 0) or fields & {"external_id", "capabilities", "context_window"}:
        linked = await db.scalar(
            select(AgentConfig.id).where(AgentConfig.default_model_id == model.id, AgentConfig.is_deleted == 0).limit(1)
        )
        if linked is not None:
            raise CustomException(status_code=409, custom_code=10005, detail="模型仍被 Agent 引用，请先更换其默认模型")
    for name in ("external_id", "display_name", "context_window", "input_price", "output_price", "price_currency", "status"):
        if name in fields:
            value = getattr(body, name)
            if name in {"external_id", "display_name", "price_currency", "status"} and value is None:
                raise CustomException(status_code=400, custom_code=10005, detail="必填字段不能为空")
            setattr(model, name, value.strip() if name in {"external_id", "display_name"} else value)
    if "capabilities" in fields:
        if body.capabilities is None:
            raise CustomException(status_code=400, custom_code=10005, detail="模型能力不能为空")
        model.capabilities = list(dict.fromkeys(body.capabilities))
        model.verified_capabilities = []
        model.capabilities_verified = False
    if "external_id" in fields:
        model.verified_capabilities = []
        model.capabilities_verified = False
    if model.status == 0:
        model.is_default = False
        model.default_slot = None
    audit(db, "model.updated", actor_id=actor.id, target_id=model.id)
    await commit_catalog(db)
    await db.refresh(model)
    return api_response(data=ModelOut.model_validate(model).model_dump(mode="json"), datetime_format=False)


@router.delete("/{model_id}", summary="删除可选模型")
async def delete_model(model_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """逻辑删除模型并清除默认标记。"""
    await db.scalar(select(ModelCatalog.id).where(ModelCatalog.id == model_id).with_for_update())
    model = await get_model_or_404(db, model_id)
    linked = await db.scalar(select(AgentConfig.id).where(AgentConfig.default_model_id == model.id, AgentConfig.is_deleted == 0).limit(1))
    if linked is not None:
        raise CustomException(status_code=409, custom_code=10005, detail="模型仍被 Agent 引用，请先更换其默认模型")
    model.is_deleted = 1
    model.status = 0
    model.is_default = False
    model.default_slot = None
    audit(db, "model.deleted", actor_id=actor.id, target_id=model.id)
    await db.commit()
    return api_response(message="删除成功")


@router.post("/{model_id}/default", summary="设置默认模型")
async def set_default_model(model_id: int, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)):
    """仅允许启用的模型和供应商连接成为默认配置。"""
    model = await get_model_or_404(db, model_id)
    provider = await get_provider_or_404(db, model.provider_id)
    if model.status != 1 or provider.status != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="停用的模型或连接不能设为默认")
    await db.execute(update(ModelCatalog).where(ModelCatalog.is_default.is_(True)).values(is_default=False, default_slot=None))
    model.is_default = True
    model.default_slot = "global"
    audit(db, "model.default_changed", actor_id=actor.id, target_id=model.id)
    await commit_catalog(db)
    await db.refresh(model)
    return api_response(data=ModelOut.model_validate(model).model_dump(mode="json"), datetime_format=False)
