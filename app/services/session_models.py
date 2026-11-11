# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 17:26:34
# @Author  : yangyuexiong
# @File    : session_models.py

"""集中维护会话模型选择、兼容性与跨模型文字上下文。"""

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.enums import MessageRole, ModelCapability, RunState, SessionModelReason
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.knowledge import AgentKnowledgeBase
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.model_adapter import thinking_request_options, validate_output_token_setting
from app.models.skill import AgentSkill
from app.models.tool import AgentTool
from app.schemas.agent_session import SessionModelOptionOut
from app.services.runtime_adapter import CONTEXT_OVERHEAD_TOKENS, MIN_CONTEXT_INPUT_TOKENS

MAX_HANDOFF_TURNS = 100
MAX_HANDOFF_CHARACTERS = 262144


def session_model_conflict(reason: SessionModelReason, detail: str) -> CustomException:
    """构造前端可按原因码处理的模型选择冲突。"""
    return CustomException(
        status_code=409,
        custom_code=10005,
        detail=detail,
        data={
            "reason": reason,
        },
    )


async def effective_session_model(db: AsyncSession, session: AgentSession, agent: AgentConfig) -> int:
    """兼容旧记录；已绑定模型不受 Agent 默认模型变更影响。"""
    if session.model_id is not None:
        return session.model_id
    for completed_only in (True, False):
        statement = select(AgentRun.model_id).where(
            AgentRun.session_id == session.id,
            AgentRun.owner_id == session.owner_id,
            AgentRun.model_id.is_not(None),
        )
        if completed_only:
            statement = statement.where(AgentRun.state == RunState.COMPLETED)
        model_id = await db.scalar(statement.order_by(AgentRun.create_time.desc(), AgentRun.id.desc()).limit(1))
        if model_id is not None:
            return model_id
    return agent.default_model_id


async def requires_tools(db: AsyncSession, agent_id: int) -> bool:
    """配置了 Tool、Skill 或知识库的 Agent 要求已验证工具能力。"""
    for binding in (AgentTool, AgentSkill, AgentKnowledgeBase):
        if await db.scalar(select(binding.id).where(binding.agent_id == agent_id).limit(1)) is not None:
            return True
    return False


def model_issue(
    model: ModelCatalog | None,
    provider: ProviderConnection | None,
    agent: AgentConfig,
    needs_tools: bool,
) -> tuple[SessionModelReason, str] | None:
    """统一返回模型不可选原因，目录和写入使用相同规则。"""
    if model is None or model.is_deleted != 0 or model.status != 1 or provider is None or provider.is_deleted != 0 or provider.status != 1:
        return SessionModelReason.UNAVAILABLE, "会话模型或供应商不可用，请选择其他模型"
    try:
        thinking_request_options(provider.kind, agent.thinking_mode)
    except CustomException as exc:
        return SessionModelReason.THINKING, exc.detail
    capabilities = model.verified_capabilities or []
    if ModelCapability.TEXT not in capabilities or needs_tools and ModelCapability.TOOLS not in capabilities:
        return SessionModelReason.CAPABILITY, "模型未通过当前 Agent 所需的文本或工具调用能力验证"
    try:
        validate_output_token_setting(provider.kind, agent.max_output_tokens)
    except CustomException as exc:
        return SessionModelReason.OUTPUT, exc.detail
    minimum_window = (agent.max_output_tokens or 0) + CONTEXT_OVERHEAD_TOKENS + MIN_CONTEXT_INPUT_TOKENS
    if model.context_window is not None and model.context_window < minimum_window:
        return SessionModelReason.CONTEXT, "模型上下文窗口无法容纳输出上限和最低输入空间"
    missing_price = any(value is None for value in (model.input_price, model.output_price, model.price_currency))
    if agent.max_estimated_cost is not None and missing_price:
        return SessionModelReason.PRICING, "启用成本预算前必须配置目标模型的输入、输出价格和币种"
    return None


async def validate_session_model(db: AsyncSession, agent: AgentConfig, model_id: int) -> ModelCatalog:
    """写入前锁定模型并复核状态、能力、窗口及费用配置。"""
    model = await db.get(ModelCatalog, model_id, with_for_update=True, populate_existing=True)
    provider = await db.get(ProviderConnection, model.provider_id) if model is not None else None
    issue = model_issue(model, provider, agent, await requires_tools(db, agent.id))
    if issue is not None:
        raise session_model_conflict(*issue)
    assert model is not None
    return model


async def session_model_options(db: AsyncSession, agent: AgentConfig) -> list[SessionModelOptionOut]:
    """返回最小模型目录和明确的禁用原因，不暴露连接凭据。"""
    rows = await db.execute(
        select(ModelCatalog, ProviderConnection)
        .join(ProviderConnection, ProviderConnection.id == ModelCatalog.provider_id)
        .where(ModelCatalog.is_deleted == 0)
        .order_by(ProviderConnection.name, ModelCatalog.id),
    )
    needs_tools = await requires_tools(db, agent.id)
    result = []
    for model, provider in rows:
        if ModelCapability.TEXT not in (model.verified_capabilities or []):
            continue
        issue = model_issue(model, provider, agent, needs_tools)
        result.append(
            SessionModelOptionOut(
                id=model.id,
                display_name=model.display_name,
                provider_name=provider.name,
                verified_capabilities=model.verified_capabilities or [],
                context_window=model.context_window,
                selectable=issue is None,
                reason=issue[0] if issue else None,
                notice=issue[1] if issue else None,
            ),
        )
    return result


async def handoff_messages(db: AsyncSession, run: AgentRun) -> list[BaseMessage]:
    """新线程只继承已完成轮次文字，不迁移工具调用、附件或供应商私有状态。"""
    if run.session_id is None:
        return []
    current_message_id = await db.scalar(select(AgentRunMessage.id).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == MessageRole.USER))
    if current_message_id is None:
        return []
    previous_runs = (
        await db.scalars(
            select(AgentRun.id)
            .join(AgentRunMessage, AgentRunMessage.run_id == AgentRun.id)
            .where(
                AgentRunMessage.role == MessageRole.USER,
                AgentRunMessage.id < current_message_id,
                AgentRun.session_id == run.session_id,
                AgentRun.owner_id == run.owner_id,
                AgentRun.agent_id == run.agent_id,
                AgentRun.state == RunState.COMPLETED,
                AgentRun.id != run.id,
            )
            .order_by(AgentRunMessage.id.desc())
            .limit(MAX_HANDOFF_TURNS + 1),
        )
    ).all()
    if not previous_runs:
        return []
    rows = (
        await db.scalars(
            select(AgentRunMessage)
            .where(
                AgentRunMessage.run_id.in_(previous_runs[:MAX_HANDOFF_TURNS]),
                AgentRunMessage.owner_id == run.owner_id,
                AgentRunMessage.id < current_message_id,
                AgentRunMessage.role.in_([MessageRole.USER, MessageRole.ASSISTANT]),
            )
            .order_by(AgentRunMessage.id),
        )
    ).all()
    # 按整轮从最近向前保留，避免出现缺少用户问题的孤立回答。
    turns: dict[str, list[AgentRunMessage]] = {}
    for message in rows:
        turns.setdefault(message.run_id, []).append(message)
    selected = []
    size = 0
    for turn in reversed(list(turns.values())):
        turn_size = sum(len(message.content) for message in turn)
        if size + turn_size > MAX_HANDOFF_CHARACTERS:
            break
        selected.append(turn)
        size += turn_size
    result: list[BaseMessage] = []
    if len(selected) < len(previous_runs):
        result.append(HumanMessage(content="会话模型已切换：较早的部分文字历史因容量限制未携带；需要时请用户补充。"))
    for turn in reversed(selected):
        for message in turn:
            message_type = HumanMessage if message.role == MessageRole.USER else AIMessage
            result.append(message_type(content=message.content))
    return result
