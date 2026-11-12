# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 21:48:20
# @Author  : yangyuexiong
# @File    : run_presentation.py

"""从现有运行与公开事件恢复会话展示数据，不读取私有检查点。"""

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import ModelThinkingMode, RunEventType, RunState
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.schemas.run import RunMetricsOut
from app.schemas.agent_session import SessionMetricsOut
from app.models.base import shanghai_now
from app.services.run_manager import TERMINAL_STATES


async def load_run_presentation(db: AsyncSession, owner_id: int, run_ids: list[str]) -> dict:
    """批量恢复授权用户的公开思考、未完成正文和轮次统计，不读取工具参数或私有检查点。"""
    if not run_ids:
        return {}
    runs = (await db.scalars(select(AgentRun).where(AgentRun.owner_id == owner_id, AgentRun.id.in_(run_ids)))).all()
    result = {}
    parts = {run.id: [] for run in runs}
    completed = {}
    text_parts = {run.id: [] for run in runs if run.state in {RunState.FAILED, RunState.TIMED_OUT, RunState.CANCELLED}}
    if not runs:
        return result
    events = (
        await db.execute(
            select(AgentRunEvent.run_id, AgentRunEvent.event_type, AgentRunEvent.state, AgentRunEvent.content, AgentRunEvent.create_time)
            .where(
                AgentRunEvent.run_id.in_(parts),
                or_(
                    AgentRunEvent.event_type.in_([RunEventType.STATE, RunEventType.REASONING_DELTA]),
                    and_(AgentRunEvent.run_id.in_(text_parts), AgentRunEvent.event_type == RunEventType.TEXT_DELTA),
                ),
            )
            .order_by(AgentRunEvent.run_id, AgentRunEvent.sequence),
        )
    ).all()
    for event in events:
        if event.event_type == RunEventType.REASONING_DELTA:
            parts[event.run_id].append(event.content or "")
        elif event.event_type == RunEventType.TEXT_DELTA:
            if event.run_id in text_parts:
                text_parts[event.run_id].append(event.content or "")
        elif event.state in TERMINAL_STATES:
            completed.setdefault(event.run_id, event.create_time)
    for run in runs:
        finished = completed.get(run.id)
        elapsed = max(0, int((finished - run.create_time).total_seconds() * 1000)) if finished is not None else None
        result[run.id] = {
            "reasoning_content": "".join(parts[run.id]) if run.agent_thinking_mode != ModelThinkingMode.DISABLED else "",
            "partial_content": "".join(text_parts.get(run.id, [])),
            "run_state": run.state,
            "failure_code": run.failure_code,
            "metrics": RunMetricsOut(elapsed_ms=elapsed, total_tokens=run.reported_total_tokens or None).model_dump(mode="json"),
        }
    return result


async def load_session_metrics(db: AsyncSession, owner_id: int, agent_id: int, session_id: str) -> SessionMetricsOut:
    """按运行去重汇总整个授权会话，不受消息或运行列表分页影响。"""
    finished = (
        select(AgentRunEvent.run_id, func.min(AgentRunEvent.create_time).label("finished_at"))
        .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
        .where(
            AgentRun.owner_id == owner_id,
            AgentRun.agent_id == agent_id,
            AgentRun.session_id == session_id,
            AgentRunEvent.event_type == RunEventType.STATE,
            AgentRunEvent.state.in_(TERMINAL_STATES),
        )
        .group_by(AgentRunEvent.run_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(AgentRun.state, AgentRun.create_time, AgentRun.reported_total_tokens, finished.c.finished_at)
            .outerjoin(finished, finished.c.run_id == AgentRun.id)
            .where(AgentRun.owner_id == owner_id, AgentRun.agent_id == agent_id, AgentRun.session_id == session_id),
        )
    ).all()
    now = shanghai_now()
    elapsed = tokens = active = missing = 0
    for row in rows:
        tokens += row.reported_total_tokens or 0
        if row.state not in TERMINAL_STATES:
            active += 1
            end = now.replace(tzinfo=None)
        elif row.finished_at is not None:
            end = row.finished_at
        else:
            missing += 1
            continue
        elapsed += max(0, int((end - row.create_time).total_seconds() * 1000))
    return SessionMetricsOut(
        run_count=len(rows),
        active_run_count=active,
        elapsed_ms=elapsed,
        total_tokens=tokens if tokens or not rows else None,
        missing_duration_runs=missing,
        calculated_at=now,
    )
