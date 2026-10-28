# -*- coding: utf-8 -*-
# @Time    : 2026-10-25 13:20:00
# @Author  : yangyuexiong
# @File    : tool_registry.py

"""为运行时工具声明权限、审批和恢复边界。"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from langchain_core.tools import BaseTool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.agent_access import can_access_run_agent

RESERVED_TOOL_NAMES = frozenset({"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute", "task", "write_todos"})
ToolEffect = Literal["read", "write"]
RecoveryPolicy = Literal["safe", "idempotent", "compensated"]


@dataclass(frozen=True)
class ToolContext:
    sessions: async_sessionmaker[AsyncSession]
    run_id: str
    owner_id: int
    lease_token: str

    async def require_active(self, db: AsyncSession, *, enabled: Callable[[AgentRun], bool]) -> AgentRun:
        """每次工具调用重新核对租约、用户、模型和当前功能开关。"""
        run = await db.get(AgentRun, self.run_id)
        owner = await db.get(Admin, self.owner_id)
        now = shanghai_now(naive=True)
        if (
            run is None or run.owner_id != self.owner_id or run.state != "running"
            or not self.lease_token or run.lease_token != self.lease_token
            or run.lease_expires_at is None or run.lease_expires_at <= now
            or not enabled(run) or owner is None or owner.status != 1 or owner.is_deleted != 0
        ):
            raise ValueError("工具授权已失效")
        if not await can_access_run_agent(db, run.agent_id, self.owner_id):
            raise ValueError("工具授权已失效")
        if run.model_id is not None:
            model = await db.get(ModelCatalog, run.model_id)
            if model is None or model.status != 1 or model.is_deleted != 0 or "tools" not in (model.verified_capabilities or []):
                raise ValueError("工具授权已失效")
            provider = await db.get(ProviderConnection, model.provider_id)
            if provider is None or provider.status != 1 or provider.is_deleted != 0:
                raise ValueError("工具授权已失效")
        return run


@dataclass(frozen=True)
class ToolSpec:
    name: str
    factory: Callable[[ToolContext], BaseTool]
    enabled: Callable[[AgentRun], bool]
    requires_approval: bool
    effect: ToolEffect
    recovery: RecoveryPolicy
    staged: bool = False


class ToolRegistry:
    """保持工具名唯一，并拒绝未声明可核对恢复策略的写工具。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]*", spec.name) is None or spec.name in RESERVED_TOOL_NAMES:
            raise ValueError("工具名无效或与运行时内置工具冲突")
        if spec.name in self._specs:
            raise ValueError("工具名重复")
        if spec.effect not in {"read", "write"} or spec.recovery not in {"safe", "idempotent", "compensated"}:
            raise ValueError("工具副作用或恢复策略无效")
        if spec.effect == "write" and (
            spec.recovery == "safe" or not spec.requires_approval
            and not (spec.staged and spec.recovery == "idempotent")
        ):
            raise ValueError("写工具必须经过审批，或仅暂存并具备幂等策略")
        self._specs[spec.name] = spec

    async def resolve(
        self, db: AsyncSession, run: AgentRun, sessions: async_sessionmaker[AsyncSession],
    ) -> tuple[list[BaseTool], dict[str, bool]]:
        selected = [spec for spec in self._specs.values() if spec.enabled(run)]
        if not selected:
            return [], {}
        context = ToolContext(sessions, run.id, run.owner_id, run.lease_token)
        await context.require_active(db, enabled=lambda current: all(spec.enabled(current) for spec in selected))
        tools = [spec.factory(context) for spec in selected]
        if any(tool.name != spec.name for tool, spec in zip(tools, selected, strict=True)):
            raise ValueError("工具实现名称与注册声明不一致")
        return tools, {spec.name: True for spec in selected if spec.requires_approval}
