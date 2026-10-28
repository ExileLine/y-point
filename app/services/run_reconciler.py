# -*- coding: utf-8 -*-
# @Time    : 2026-10-23 14:21:55
# @Author  : yangyuexiong
# @File    : run_reconciler.py

"""从已完成的持久化检查点补偿租约过期的文本运行。"""

from pathlib import Path

from deepagents import create_deep_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.base import shanghai_now
from app.services.run_manager import RunConflictError, complete_expired_run
from app.services.runtime_checkpoint import open_async_checkpointer


class CheckpointReadModel(BaseChatModel):
    """只供 DeepAgents 重建检查点状态使用，任何模型调用都会失败。"""

    @property
    def _llm_type(self) -> str:
        """标识只读检查点模型。"""
        return "checkpoint_read_only"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs) -> ChatResult:
        """拒绝补偿流程意外发起模型调用。"""
        raise RuntimeError("补偿流程不允许调用模型")

    def bind_tools(self, tools, **kwargs) -> BaseChatModel:
        """构建只读图时接受工具绑定，但不开放执行能力。"""
        return self


async def reconcile_expired_runs(sessions: async_sessionmaker[AsyncSession], checkpoint_path: Path, *, limit: int = 100) -> int:
    """只补写能由当前 Run 的最终检查点证明已完成的过期运行。"""
    if not 1 <= limit <= 1000:
        raise ValueError("无效的检查点补偿数量")
    async with sessions() as db:
        statement = (
            select(AgentRun.id, AgentRun.owner_id, AgentRun.thread_id)
            .where(AgentRun.state == "running", AgentRun.lease_expires_at <= shanghai_now(naive=True))
            .order_by(AgentRun.lease_expires_at, AgentRun.id)
            .limit(limit)
        )
        candidates = (await db.execute(statement)).all()
    if not candidates:
        return 0

    completed = 0
    async with open_async_checkpointer(checkpoint_path) as checkpointer:
        agent = create_deep_agent(model=CheckpointReadModel(), tools=[], checkpointer=checkpointer)
        for run_id, owner_id, thread_id in candidates:
            try:
                snapshot = await agent.aget_state(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                        },
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单个损坏的检查点不阻止其他运行回收。
                logger.warning("运行 {} 的检查点读取失败：{}", run_id, type(exc).__name__)
                continue
            checkpoint_id = snapshot.config.get("configurable", {}).get("checkpoint_id")
            messages = snapshot.values.get("messages", [])
            metadata = snapshot.metadata or {}
            if (
                not checkpoint_id
                or metadata.get("run_id") != run_id
                or metadata.get("tool_count") != 0
                or snapshot.next
                or not messages
                or not isinstance(messages[-1], AIMessage)
                or messages[-1].tool_calls
                or messages[-1].invalid_tool_calls
            ):
                continue
            final_text = str(messages[-1].text)
            if not final_text.strip() or len(final_text) > 262144:
                continue
            try:
                async with sessions.begin() as db:
                    user = await db.get(Admin, owner_id)
                    if user is None or user.status != 1 or user.is_deleted != 0:
                        continue
                    existing = await db.scalar(
                        select(AgentRunMessage.id).where(AgentRunMessage.run_id == run_id, AgentRunMessage.role == "assistant").limit(1)
                    )
                    if existing is not None:
                        continue
                    await complete_expired_run(db, run_id, checkpoint_id=checkpoint_id, final_text=final_text)
                completed += 1
            except RunConflictError:
                continue
    return completed
