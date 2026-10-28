"""Agent 对指定用户的使用授权。"""

from sqlalchemy import Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import CustomBaseModel


class AgentShare(CustomBaseModel):
    __table_name__ = "agent_share"
    __table_args__ = (
        UniqueConstraint("agent_id", "user_id", name="uq_yp_agent_share_agent_user"),
        {"comment": "智能体指定用户授权表"},
    )

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="被共享智能体 ID")
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="获授权用户 ID")
