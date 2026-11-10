# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 00:59:16
# @Author  : yangyuexiong
# @File    : run_calls.py

"""将已有公开工具事件汇总为会话可展示的脱敏调用记录。"""

import re

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunCallKind, RunCallStatus, RunEventType, Visibility
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.knowledge import KnowledgeBase, RunKnowledgeBase
from app.models.skill import Skill, RunSkill
from app.models.skill_package import SkillScript
from app.models.tool import RunTool, UserTool
from app.services.user_tools import model_tool_name
from app.schemas.run import RunCallOut
from app.services.knowledge import knowledge_access
from app.services.run_manager import TERMINAL_STATES

TOOL_EVENTS = (RunEventType.TOOL_STARTED, RunEventType.TOOL_COMPLETED, RunEventType.TOOL_FAILED)


async def load_run_calls(db: AsyncSession, owner_id: int, run_ids: list[str]) -> dict[str, list[RunCallOut]]:
    """批量读取已授权运行的调用事件，调用方须先复核会话或 Agent 当前访问权。"""
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(AgentRunEvent, AgentRun.state)
            .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
            .where(
                AgentRun.owner_id == owner_id,
                AgentRun.id.in_(run_ids),
                AgentRunEvent.event_type.in_(TOOL_EVENTS),
                AgentRunEvent.tool_call_id.is_not(None),
            )
            .order_by(AgentRunEvent.run_id, AgentRunEvent.sequence),
        )
    ).all()
    knowledge_runs = {event.run_id for event, _ in rows if event.tool_name == "search_knowledge_base"}
    knowledge_names: dict[str, list[str]] = {}
    if knowledge_runs:
        targets = (
            await db.execute(
                select(RunKnowledgeBase.run_id, KnowledgeBase.name)
                .outerjoin(
                    KnowledgeBase,
                    and_(KnowledgeBase.id == RunKnowledgeBase.knowledge_base_id, knowledge_access(owner_id)),
                )
                .where(RunKnowledgeBase.run_id.in_(knowledge_runs), RunKnowledgeBase.index_id.is_not(None))
                .order_by(RunKnowledgeBase.id),
            )
        ).all()
        for run_id, name in targets:
            knowledge_names.setdefault(run_id, []).append(name or "知识库（已不可用）")
    script_names = {}
    managed = (
        await db.execute(
            select(RunTool, UserTool, SkillScript, Skill, RunSkill.skill_version)
            .join(UserTool, UserTool.id == RunTool.tool_id)
            .join(SkillScript, and_(SkillScript.tool_id == RunTool.tool_id, SkillScript.package_hash == RunTool.content_hash))
            .join(Skill, Skill.id == SkillScript.skill_id)
            .join(RunSkill, and_(RunSkill.run_id == RunTool.run_id, RunSkill.skill_id == Skill.id))
            .where(RunTool.run_id.in_(run_ids)),
        )
    ).all()
    for fixed, tool, binding, skill, version in managed:
        visible = not skill.is_deleted and skill.enabled and (skill.owner_id == owner_id or skill.visibility == Visibility.PUBLIC)
        script_names[(fixed.run_id, model_tool_name(tool))] = f"{skill.name}@v{version} / {binding.path}" if visible else "Skill 脚本（已不可用）"
    grouped: dict[str, dict[str, RunCallOut]] = {}
    for event, run_state in rows:
        calls = grouped.setdefault(event.run_id, {})
        call = calls.get(event.tool_call_id)
        if call is None:
            tool_name = event.tool_name or "未命名工具"
            if tool_name.startswith("skill:"):
                kind, name = RunCallKind.SKILL, tool_name.removeprefix("skill:")
            elif tool_name == "search_knowledge_base":
                kind = RunCallKind.KNOWLEDGE
                name = "、".join(knowledge_names.get(event.run_id, [])) or "知识库检索"
            else:
                kind, name = RunCallKind.TOOL, script_names.get((event.run_id, tool_name), re.sub(r"^user_tool_\d+_", "", tool_name))
            call = RunCallOut(
                call_id=event.tool_call_id,
                kind=kind,
                name=name,
                status=RunCallStatus.INTERRUPTED if run_state in TERMINAL_STATES else RunCallStatus.RUNNING,
                sequence=event.sequence,
            )
            calls[event.tool_call_id] = call
        if event.event_type == RunEventType.TOOL_COMPLETED:
            call.status = RunCallStatus.SUCCEEDED
        elif event.event_type == RunEventType.TOOL_FAILED:
            call.status = RunCallStatus.FAILED
    return {run_id: list(calls.values()) for run_id, calls in grouped.items()}
