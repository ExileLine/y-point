# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 23:29:59
# @Author  : yangyuexiong
# @File    : run_recovery.py

"""证明中断轮次可安全回退，并为主动取消重建有界公开文字上下文，不重放工具或修改原检查点。"""

from collections import Counter

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ArtifactOrigin, MessageRole, RunEventType, RunState, ToolEffect, ToolInvocationState
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.artifact import Artifact
from app.models.skill import RunSkill
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services.user_tools import model_tool_name


MAX_CANCELLED_CONTEXT_TURNS = 100
MAX_CANCELLED_CONTEXT_CHARACTERS = 262144
CANCELLED_CONTEXT_NOTE = (
    "[本轮由用户主动取消，以上仅为已向用户公开的未完成正文，不代表最终回答或任务已完成。"
    "尚未闭合的中间工具调用已从上下文移除，不得自动续跑旧调用。"
    "取消不撤销已发生的操作；涉及外部写入时先核实结果，结果未确认时不得重做。"
    "用户随后要求继续或追问时，结合原提问与上述正文衔接；没有生成文件不等于没有对话上下文。]"
)


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


async def cancelled_run_messages(
    db: AsyncSession,
    run: AgentRun,
    user_message: AgentRunMessage,
    *,
    max_characters: int = MAX_CANCELLED_CONTEXT_CHARACTERS,
) -> list[BaseMessage]:
    """仅将取消轮的原提问、已公开正文及中断说明转换为普通文字消息，不导入检查点或工具消息。"""
    if run.state != RunState.CANCELLED or user_message.run_id != run.id or user_message.owner_id != run.owner_id or user_message.role != MessageRole.USER:
        return []
    truncated_note = "[较长的已公开正文因上下文容量限制截断。]"
    remaining = max_characters - len(user_message.content) - len(CANCELLED_CONTEXT_NOTE) - len(truncated_note) - 2
    if remaining < 0:
        return []
    parts = []
    size = 0
    separated = False
    events = await db.stream_scalars(
        select(AgentRunEvent)
        .where(
            AgentRunEvent.run_id == run.id,
            AgentRunEvent.sequence <= run.event_seq,
            AgentRunEvent.event_type.in_((RunEventType.TEXT_DELTA, RunEventType.MODEL_STARTED)),
        )
        .order_by(AgentRunEvent.sequence),
    )
    try:
        async for event in events:
            if event.event_type == RunEventType.MODEL_STARTED:
                separated = bool(parts)
                continue
            if not event.content:
                continue
            content = ("\n\n" if separated else "") + event.content
            separated = False
            if size + len(content) > remaining:
                parts.append(content[: remaining - size])
                parts.append(truncated_note)
                break
            parts.append(content)
            size += len(content)
    finally:
        await events.close()
    public_text = "".join(parts)
    return [
        HumanMessage(content=user_message.content, id=f"cancelled:{run.id}:user"),
        AIMessage(content=(public_text + "\n\n" if public_text else "") + CANCELLED_CONTEXT_NOTE, id=f"cancelled:{run.id}:assistant"),
    ]


async def cancelled_messages_since(db: AsyncSession, run: AgentRun, previous_run: AgentRun | None) -> list[BaseMessage]:
    """补回当前干净检查点之后、当前输入之前的同属主同线程取消轮，按整轮控制容量且避免重复。"""
    current_id = await db.scalar(select(AgentRunMessage.id).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == MessageRole.USER))
    if current_id is None:
        return []
    statement = (
        select(AgentRun, AgentRunMessage)
        .join(AgentRunMessage, AgentRunMessage.run_id == AgentRun.id)
        .where(
            AgentRun.owner_id == run.owner_id,
            AgentRun.agent_id == run.agent_id,
            AgentRun.session_id == run.session_id,
            AgentRun.thread_id == run.thread_id,
            AgentRun.state == RunState.CANCELLED,
            AgentRunMessage.owner_id == run.owner_id,
            AgentRunMessage.role == MessageRole.USER,
            AgentRunMessage.id < current_id,
        )
    )
    if previous_run is not None:
        previous_id = await db.scalar(
            select(AgentRunMessage.id).where(AgentRunMessage.run_id == previous_run.id, AgentRunMessage.role == MessageRole.USER),
        )
        if previous_id is None:
            return []
        statement = statement.where(AgentRunMessage.id > previous_id)
    rows = (await db.execute(statement.order_by(AgentRunMessage.id.desc()).limit(MAX_CANCELLED_CONTEXT_TURNS + 1))).all()
    omitted_note = "上下文容量限制：部分较早的取消轮次未携带，需要时请用户补充。"
    available_characters = MAX_CANCELLED_CONTEXT_CHARACTERS - len(omitted_note)
    selected = []
    size = 0
    for cancelled, message in rows[:MAX_CANCELLED_CONTEXT_TURNS]:
        messages = await cancelled_run_messages(db, cancelled, message, max_characters=available_characters - size)
        if not messages:
            break
        selected.append(messages)
        size += sum(len(item.content) for item in messages)
    result = []
    if len(selected) < len(rows):
        result.append(HumanMessage(content=omitted_note))
    for messages in reversed(selected):
        result.extend(messages)
    return result
