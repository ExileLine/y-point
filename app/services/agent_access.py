# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 14:25:00
# @Author  : yangyuexiong
# @File    : agent_access.py

"""Agent 配置的所有者和指定用户授权检查。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_config import AgentConfig
from app.models.agent_share import AgentShare


async def can_use_agent(db: AsyncSession, agent_id: int, user_id: int) -> bool:
    """检查用户是否拥有智能体或具有有效的指定用户授权。"""
    agent = await db.get(AgentConfig, agent_id)
    if agent is None:
        return False
    if agent.owner_id == user_id:
        return True
    if agent.is_deleted != 0:
        return False
    return await db.scalar(select(AgentShare.id).where(AgentShare.agent_id == agent_id, AgentShare.user_id == user_id).limit(1)) is not None


async def can_access_run_agent(db: AsyncSession, agent_id: int | None, user_id: int) -> bool:
    """按运行所引用的智能体复核当前用户使用权限。"""
    return agent_id is None or await can_use_agent(db, agent_id, user_id)
