# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 11:00:31
# @Author  : yangyuexiong
# @File    : agents.py

"""当前用户的私有 Agent 配置管理。"""

from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.agent import AgentCreateIn, AgentOut, AgentPageOut, AgentUpdateIn
from app.services.identity import audit
from app.services.runtime_adapter import CONTEXT_OVERHEAD_TOKENS, MIN_CONTEXT_INPUT_TOKENS


router = APIRouter(prefix="/agents", tags=["agents"])


async def _available_text_model(db: AsyncSession, model_id: int, max_output_tokens: int) -> ModelCatalog:
    """确认默认模型及供应商可用，且模型已经通过文本能力验证。"""
    model = await db.get(ModelCatalog, model_id)
    provider = await db.get(ProviderConnection, model.provider_id) if model is not None else None
    if model is None or model.is_deleted != 0 or model.status != 1 or provider is None or provider.is_deleted != 0 or provider.status != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="默认模型或供应商不可用")
    if "text" not in (model.verified_capabilities or []):
        raise CustomException(status_code=409, custom_code=10005, detail="默认模型尚未通过文本能力验证")
    if model.context_window is not None and (
        model.context_window - max_output_tokens < CONTEXT_OVERHEAD_TOKENS + MIN_CONTEXT_INPUT_TOKENS
    ):
        raise CustomException(status_code=409, custom_code=10005, detail="模型上下文窗口无法容纳输出上限和最低输入空间")
    return model


def _check_cost_pricing(model: ModelCatalog, max_estimated_cost: Decimal | None) -> None:
    """启用估算成本预算时要求当前模型有完整价格。"""
    if max_estimated_cost is not None and (model.input_price is None or model.output_price is None):
        raise CustomException(status_code=409, custom_code=10005, detail="估算成本预算要求模型配置输入和输出价格")


async def _owned_agent(db: AsyncSession, agent_id: int, owner_id: int) -> AgentConfig:
    """只读取当前用户仍有效的 Agent，跨用户访问与不存在统一返回 404。"""
    agent = await db.scalar(select(AgentConfig).where(
        AgentConfig.id == agent_id, AgentConfig.owner_id == owner_id, AgentConfig.is_deleted == 0,
    ))
    if agent is None:
        raise CustomException(status_code=404, custom_code=10002, detail="智能体不存在")
    return agent


@router.post("", summary="创建私有智能体")
async def create_agent(body: AgentCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """验证默认模型后保存当前用户的私有 Agent 配置。"""
    model = await _available_text_model(db, body.default_model_id, body.max_output_tokens)
    _check_cost_pricing(model, body.max_estimated_cost)
    agent = AgentConfig(owner_id=actor.id, **body.model_dump(), version=1, is_deleted=0, status=1)
    db.add(agent)
    await db.flush()
    audit(db, "agent.created", actor_id=actor.id, target_id=agent.id)
    await db.commit()
    await db.refresh(agent)
    return api_response(http_code=201, code=201, data=AgentOut.model_validate(agent).model_dump(mode="json"), datetime_format=False)


@router.get("", summary="分页查询我的智能体")
async def list_agents(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session),
):
    """仅分页展示当前用户未删除的 Agent 配置。"""
    condition = (AgentConfig.owner_id == actor.id, AgentConfig.is_deleted == 0)
    total = await db.scalar(select(func.count()).select_from(AgentConfig).where(*condition))
    items = (await db.scalars(select(AgentConfig).where(*condition).order_by(AgentConfig.id.desc())
        .offset((page - 1) * page_size).limit(page_size))).all()
    result = AgentPageOut(items=[AgentOut.model_validate(item) for item in items], total=total or 0, page=page, page_size=page_size)
    return api_response(data=result.model_dump(mode="json"), datetime_format=False)


@router.get("/{agent_id}", summary="查询我的智能体详情")
async def get_agent(agent_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """读取当前用户的指定 Agent 配置。"""
    agent = await _owned_agent(db, agent_id, actor.id)
    return api_response(data=AgentOut.model_validate(agent).model_dump(mode="json"), datetime_format=False)


@router.put("/{agent_id}", summary="修改我的智能体")
async def update_agent(
    agent_id: int, body: AgentUpdateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session),
):
    """使用期望版本原子更新配置，避免覆盖其他请求的编辑。"""
    agent = await _owned_agent(db, agent_id, actor.id)
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    required = {
        "name", "system_prompt", "default_model_id", "temperature", "max_output_tokens",
        "model_timeout_seconds", "tool_timeout_seconds", "enabled",
    }
    if any(changes.get(name) is None for name in required & changes.keys()):
        raise CustomException(status_code=400, custom_code=10005, detail="必填配置不能为空")
    if {"default_model_id", "max_output_tokens", "max_estimated_cost"} & changes.keys() or changes.get("enabled") is True:
        model = await _available_text_model(
            db, changes.get("default_model_id", agent.default_model_id),
            changes.get("max_output_tokens", agent.max_output_tokens),
        )
        _check_cost_pricing(model, changes.get("max_estimated_cost", agent.max_estimated_cost))
    if changes:
        result = await db.execute(update(AgentConfig).where(
            AgentConfig.id == agent_id, AgentConfig.owner_id == actor.id,
            AgentConfig.is_deleted == 0, AgentConfig.version == body.expected_version,
        ).values(**changes, version=AgentConfig.version + 1).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            await db.rollback()
            raise CustomException(status_code=409, custom_code=10005, detail="智能体配置版本已变化")
        audit(db, "agent.updated", actor_id=actor.id, target_id=agent_id)
        await db.commit()
        await db.refresh(agent)
    elif agent.version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="智能体配置版本已变化")
    return api_response(data=AgentOut.model_validate(agent).model_dump(mode="json"), datetime_format=False)


@router.delete("/{agent_id}", summary="删除我的智能体")
async def delete_agent(agent_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """逻辑删除当前用户的 Agent，保留配置记录供后续运行引用。"""
    agent = await _owned_agent(db, agent_id, actor.id)
    result = await db.execute(update(AgentConfig).where(
        AgentConfig.id == agent_id, AgentConfig.owner_id == actor.id,
        AgentConfig.is_deleted == 0, AgentConfig.version == agent.version,
    ).values(is_deleted=1, enabled=False, version=AgentConfig.version + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="智能体配置版本已变化")
    audit(db, "agent.deleted", actor_id=actor.id, target_id=agent_id)
    await db.commit()
    return api_response(message="智能体已删除")
