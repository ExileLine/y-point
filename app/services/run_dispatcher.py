# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 22:16:25
# @Author  : yangyuexiong
# @File    : run_dispatcher.py

"""从持久化队列中发现并执行一个待处理运行。"""

from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.run_approval import RunApproval
from app.services.model_adapter import build_chat_model
from app.services.run_executor import ModelResolver, ToolResolver, execute_text_run
from app.services.run_manager import RunConflictError


async def resolve_stored_model(db: AsyncSession, run: AgentRun) -> BaseChatModel:
    """在领取事务中校验用户、模型与供应商状态并构造聊天模型。"""
    user = await db.get(Admin, run.owner_id)
    if user is None or user.status != 1 or user.is_deleted != 0 or run.model_id is None:
        raise RunConflictError("用户或模型不可用")
    model = await db.get(ModelCatalog, run.model_id)
    if model is None or model.status != 1 or model.is_deleted != 0:
        raise RunConflictError("模型不可用")
    provider = await db.get(ProviderConnection, model.provider_id)
    if provider is None or provider.status != 1 or provider.is_deleted != 0:
        raise RunConflictError("供应商连接不可用")
    return build_chat_model(
        model, provider, temperature=run.agent_temperature, max_output_tokens=run.agent_max_output_tokens,
    )


async def dispatch_next_run(
    sessions: async_sessionmaker[AsyncSession], checkpoint_path: Path, *, resolve_model: ModelResolver = resolve_stored_model,
    resolve_tools: ToolResolver | None = None, timeout_seconds: float = 120, lease_seconds: int = 30, poll_seconds: float = 5,
) -> AgentRun | None:
    """读取最早排队或可安全恢复的运行；竞争失败由下一轮处理。"""
    async with sessions() as db:
        eligible = AgentRun.state == "queued"
        if resolve_tools is not None:
            approved = select(RunApproval.id).where(
                RunApproval.run_id == AgentRun.id, RunApproval.checkpoint_id == AgentRun.checkpoint_id,
                RunApproval.state == "approved",
            ).exists()
            eligible = or_(eligible, and_(AgentRun.state == "waiting_for_approval", approved))
        statement = select(AgentRun.id, AgentRun.state).where(eligible).order_by(AgentRun.create_time, AgentRun.id).limit(1)
        candidate = (await db.execute(statement)).first()
    if candidate is None:
        return None
    try:
        return await execute_text_run(
            sessions, candidate.id, None, checkpoint_path, resolve_model=resolve_model,
            resolve_tools=resolve_tools, resume=candidate.state == "waiting_for_approval",
            timeout_seconds=timeout_seconds, lease_seconds=lease_seconds, poll_seconds=poll_seconds,
        )
    except RunConflictError:
        return None
