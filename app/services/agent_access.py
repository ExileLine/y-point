"""Agent 配置的所有者和指定用户授权检查。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_config import AgentConfig
from app.models.agent_share import AgentShare


async def can_use_agent(db: AsyncSession, agent_id: int, user_id: int) -> bool:
    agent = await db.get(AgentConfig, agent_id)
    if agent is None:
        return False
    if agent.owner_id == user_id:
        return True
    if agent.is_deleted != 0:
        return False
    return await db.scalar(select(AgentShare.id).where(
        AgentShare.agent_id == agent_id, AgentShare.user_id == user_id,
    ).limit(1)) is not None


async def can_access_run_agent(db: AsyncSession, agent_id: int | None, user_id: int) -> bool:
    return agent_id is None or await can_use_agent(db, agent_id, user_id)
