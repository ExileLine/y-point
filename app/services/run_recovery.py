# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 23:29:59
# @Author  : yangyuexiong
# @File    : run_recovery.py

"""用持久化调用与审计证明失败轮次可安全放弃，不重放工具或修改原检查点。"""

from collections import Counter

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ArtifactOrigin, RunEventType, ToolEffect, ToolInvocationState
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.artifact import Artifact
from app.models.skill import RunSkill
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services.user_tools import model_tool_name


async def can_rewind_failed_run(db: AsyncSession, failed_run: AgentRun) -> bool:
    """允许有结束事件的历史/产物回读和知识检索、完整审计只读调用及无产物失败报告退回干净检查点。"""
    events = (
        await db.scalars(
            select(AgentRunEvent)
            .where(
                AgentRunEvent.run_id == failed_run.id,
                AgentRunEvent.event_type.in_((RunEventType.TOOL_STARTED, RunEventType.TOOL_COMPLETED, RunEventType.TOOL_FAILED)),
            )
            .order_by(AgentRunEvent.sequence),
        )
    ).all()
    if failed_run.tool_call_count == 0:
        return not events
    has_skills = bool(await db.scalar(select(RunSkill.id).where(RunSkill.run_id == failed_run.id).limit(1)))
    definitions = (
        await db.execute(
            select(UserTool, RunTool.tool_version)
            .join(
                RunTool,
                RunTool.tool_id == UserTool.id,
            )
            .join(
                UserToolVersion,
                and_(UserToolVersion.tool_id == RunTool.tool_id, UserToolVersion.version == RunTool.tool_version),
            )
            .where(
                RunTool.run_id == failed_run.id,
                UserToolVersion.effect == ToolEffect.READ,
                UserToolVersion.verified.is_(True),
                UserToolVersion.content_hash == RunTool.content_hash,
            ),
        )
    ).all()
    read_tools = {model_tool_name(tool): (tool.id, version) for tool, version in definitions}
    calls = {}
    for event in events:
        if not event.tool_call_id or not event.tool_name:
            return False
        if event.event_type == RunEventType.TOOL_STARTED:
            if event.tool_call_id in calls:
                return False
            calls[event.tool_call_id] = [event.tool_name, None]
        else:
            current = calls.get(event.tool_call_id)
            if current is None or current[0] != event.tool_name or current[1] is not None:
                return False
            current[1] = event.event_type
    if len(calls) != failed_run.tool_call_count:
        return False
    expected_audits = Counter()
    has_failed_report = False
    for name, ending in calls.values():
        if name == "list_session_artifacts" or has_skills and name.startswith("skill:"):
            continue
        # 历史/产物回读和知识检索只产生读取或检索审计；成功或明确失败均不会产生外部写副作用。
        if name in {"read_file", "read_generated_artifact", "search_knowledge_base"} and ending is not None:
            continue
        if name in read_tools and ending is not None:
            expected_state = ToolInvocationState.COMPLETED if ending == RunEventType.TOOL_COMPLETED else ToolInvocationState.FAILED
            expected_audits[(*read_tools[name], expected_state)] += 1
        elif has_skills and name == "render_skill_document" and ending == RunEventType.TOOL_FAILED:
            has_failed_report = True
        else:
            return False
    audits = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == failed_run.id))).all()
    if any(item.state not in (ToolInvocationState.COMPLETED, ToolInvocationState.FAILED) for item in audits):
        return False
    actual_audits = Counter((item.tool_id, item.tool_version, item.state) for item in audits)
    if actual_audits != expected_audits:
        return False
    if has_failed_report:
        artifact = await db.scalar(select(Artifact.id).where(Artifact.run_id == failed_run.id, Artifact.source == ArtifactOrigin.GENERATED).limit(1))
        if artifact is not None:
            return False
    return True
