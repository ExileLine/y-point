# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:58:51
# @Author  : yangyuexiong
# @File    : run_manager.py

"""运行任务的持久化状态、租约和事件游标管理。"""

from datetime import datetime, timedelta
import re
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.run_approval import RunApproval
from app.models.base import shanghai_now


class RunConflictError(Exception):
    """表示运行状态已变化或执行器不再持有有效租约。"""


class RunNotFoundError(Exception):
    """表示运行不存在或不属于当前用户。"""


TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "timed_out"})
ALLOWED_TRANSITIONS = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"waiting_for_approval", "completed", "failed", "cancel_requested", "timed_out"}),
    "waiting_for_approval": frozenset({"running", "cancelled", "timed_out"}),
    "cancel_requested": frozenset({"cancelled", "failed", "timed_out"}),
}


async def create_run(
    db: AsyncSession, owner_id: int, *, agent_id: int | None = None, model_id: int | None = None,
    thread_id: str | None = None, request_key: str | None = None, input_text: str | None = None,
    model_timeout_seconds: int = 120,
) -> AgentRun:
    """在当前事务中创建运行、调度意图和可选的用户输入。"""
    if owner_id <= 0 or request_key is not None and (not request_key or len(request_key) > 128):
        raise ValueError("无效的运行所有者或提交键")
    if thread_id is not None and (not thread_id or len(thread_id) > 36):
        raise ValueError("无效的检查点线程 ID")
    if input_text is not None and (not input_text.strip() or len(input_text) > 32768):
        raise ValueError("用户输入为空或超过长度限制")
    if not 1 <= model_timeout_seconds <= 120:
        raise ValueError("模型生成超时秒数必须在 1 到 120 之间")
    if request_key is not None:
        existing = await db.scalar(select(AgentRun).where(AgentRun.owner_id == owner_id, AgentRun.request_key == request_key))
        if existing is not None:
            if (
                existing.agent_id != agent_id or existing.model_id != model_id
                or existing.model_timeout_seconds != model_timeout_seconds
                or thread_id is not None and existing.thread_id != thread_id
            ):
                raise RunConflictError("同一提交键的运行配置不一致")
            if input_text is not None:
                saved_input = await db.scalar(select(AgentRunMessage.content).where(
                    AgentRunMessage.run_id == existing.id, AgentRunMessage.role == "user",
                ))
                if saved_input != input_text:
                    raise RunConflictError("同一提交键的用户输入不一致")
            return existing
    if thread_id is not None:
        thread_owner = await db.scalar(select(AgentRun.owner_id).where(AgentRun.thread_id == thread_id).limit(1))
        if thread_owner != owner_id:
            raise RunConflictError("检查点线程不属于当前用户")
        active_run = await db.scalar(select(AgentRun.id).where(
            AgentRun.thread_id == thread_id, AgentRun.state.not_in(TERMINAL_STATES),
        ).limit(1))
        if active_run is not None:
            raise RunConflictError("同一检查点线程已有未结束的运行")
    run = AgentRun(
        id=str(uuid4()), owner_id=owner_id, agent_id=agent_id, model_id=model_id, thread_id=thread_id or str(uuid4()),
        request_key=request_key, model_timeout_seconds=model_timeout_seconds, state="queued", version=0, event_seq=1,
    )
    db.add(run)
    db.add(AgentRunEvent(run_id=run.id, sequence=1, protocol_version=1, event_type="state", state="queued"))
    if input_text is not None:
        db.add(AgentRunMessage(run_id=run.id, owner_id=owner_id, role="user", content=input_text))
    await db.flush()
    return run


async def get_owned_run(db: AsyncSession, run_id: str, owner_id: int) -> AgentRun:
    """只读取指定用户拥有的运行，避免跨用户访问。"""
    run = await db.scalar(select(AgentRun).where(AgentRun.id == run_id, AgentRun.owner_id == owner_id))
    if run is None:
        raise RunNotFoundError("运行不存在")
    return run


async def replay_events(db: AsyncSession, run_id: str, owner_id: int, *, after: int = 0, limit: int = 100) -> list[AgentRunEvent]:
    """按单调序号读取当前用户的运行事件，供断线重连补发。"""
    if after < 0 or not 1 <= limit <= 200:
        raise ValueError("无效的事件游标或数量")
    await get_owned_run(db, run_id, owner_id)
    statement = select(AgentRunEvent).where(AgentRunEvent.run_id == run_id, AgentRunEvent.sequence > after)
    return list((await db.scalars(statement.order_by(AgentRunEvent.sequence).limit(limit))).all())


async def get_owned_messages(db: AsyncSession, run_id: str, owner_id: int) -> list[AgentRunMessage]:
    """只向运行所有者返回用户输入和最终可见回答。"""
    await get_owned_run(db, run_id, owner_id)
    statement = select(AgentRunMessage).where(AgentRunMessage.run_id == run_id).order_by(AgentRunMessage.id)
    return list((await db.scalars(statement)).all())


async def _change_state(
    db: AsyncSession, run: AgentRun, state: str, *, now: datetime, lease_token: str | None = None,
    lease_seconds: int | None = None, checkpoint_id: str | None = None, failure_code: str | None = None,
    require_lease: bool = False, require_expired_lease: bool = False,
) -> AgentRun:
    """通过版本条件原子改变状态，并在同一事务追加一个脱敏事件。"""
    if state not in ALLOWED_TRANSITIONS.get(run.state, ()):
        raise RunConflictError("不允许的运行状态转换")
    if require_lease and (not lease_token or run.lease_token != lease_token or run.lease_expires_at is None or run.lease_expires_at <= now):
        raise RunConflictError("执行器租约已失效")
    if require_expired_lease and (run.state != "running" or run.lease_expires_at is None or run.lease_expires_at > now):
        raise RunConflictError("运行租约尚未过期")
    if lease_seconds is not None and lease_seconds <= 0:
        raise ValueError("租约时长必须大于零")
    values = {
        "state": state, "version": run.version + 1, "event_seq": run.event_seq + 1,
        "lease_token": lease_token if state == "running" else run.lease_token if state == "cancel_requested" else None,
        "lease_expires_at": (
            now + timedelta(seconds=lease_seconds) if state == "running" and lease_seconds else
            run.lease_expires_at if state == "cancel_requested" else None
        ),
        "heartbeat_at": now if state == "running" else run.heartbeat_at,
        "checkpoint_id": checkpoint_id if checkpoint_id is not None else run.checkpoint_id,
        "failure_code": failure_code,
    }
    statement = update(AgentRun).where(AgentRun.id == run.id, AgentRun.version == run.version, AgentRun.state == run.state)
    if require_lease:
        statement = statement.where(AgentRun.lease_token == lease_token, AgentRun.lease_expires_at > now)
    if require_expired_lease:
        statement = statement.where(AgentRun.lease_expires_at <= now)
    result = await db.execute(statement.values(**values).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise RunConflictError("运行状态已被其他执行器更新")
    db.add(AgentRunEvent(
        run_id=run.id, sequence=run.event_seq + 1, protocol_version=1, event_type="state", state=state,
        failure_code=failure_code,
    ))
    await db.flush()
    await db.refresh(run)
    return run


async def claim_run(db: AsyncSession, run_id: str, *, lease_seconds: int = 30) -> AgentRun:
    """原子领取排队任务；领取者持有返回的租约令牌。"""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    return await _change_state(db, run, "running", now=shanghai_now(naive=True), lease_token=str(uuid4()), lease_seconds=lease_seconds)


async def heartbeat_run(db: AsyncSession, run_id: str, lease_token: str, *, lease_seconds: int = 30) -> AgentRun:
    """仅允许当前执行器延长尚未过期的运行租约。"""
    if lease_seconds <= 0:
        raise ValueError("租约时长必须大于零")
    run = await db.get(AgentRun, run_id)
    now = shanghai_now(naive=True)
    if run is None or run.state != "running" or run.lease_token != lease_token:
        raise RunConflictError("执行器未持有运行租约")
    statement = update(AgentRun).where(
        AgentRun.id == run_id, AgentRun.version == run.version, AgentRun.state == "running",
        AgentRun.lease_token == lease_token, AgentRun.lease_expires_at > now,
    ).values(version=run.version + 1, heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
    result = await db.execute(statement.execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise RunConflictError("执行器租约已失效")
    await db.refresh(run)
    return run


async def append_text_delta(db: AsyncSession, run_id: str, lease_token: str, content: str) -> AgentRunEvent:
    """仅由有效执行器持久化可公开文本片段和单调游标。"""
    if not content or len(content) > 2048:
        raise ValueError("文本片段为空或超过长度限制")
    run = await db.get(AgentRun, run_id)
    now = shanghai_now(naive=True)
    if (
        run is None or run.state != "running" or run.lease_token != lease_token
        or run.lease_expires_at is None or run.lease_expires_at <= now
    ):
        raise RunConflictError("执行器租约已失效")
    statement = update(AgentRun).where(
        AgentRun.id == run_id, AgentRun.state == "running", AgentRun.version == run.version,
        AgentRun.lease_token == lease_token, AgentRun.lease_expires_at > now,
    ).values(version=run.version + 1, event_seq=run.event_seq + 1)
    result = await db.execute(statement.execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise RunConflictError("运行状态已被其他执行器更新")
    event = AgentRunEvent(
        run_id=run_id, sequence=run.event_seq + 1, protocol_version=1, event_type="text_delta",
        state="running", content=content,
    )
    db.add(event)
    await db.flush()
    await db.refresh(run)
    return event


async def complete_run(
    db: AsyncSession, run_id: str, lease_token: str, *, checkpoint_id: str | None = None, final_text: str | None = None
) -> AgentRun:
    """由持有租约的执行器在同一事务保存最终回答和完成状态。"""
    if final_text is not None and (not final_text.strip() or len(final_text) > 262144):
        raise ValueError("最终回答为空或超过长度限制")
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    completed = await _change_state(
        db, run, "completed", now=shanghai_now(naive=True), lease_token=lease_token,
        checkpoint_id=checkpoint_id, require_lease=True,
    )
    if final_text is not None:
        db.add(AgentRunMessage(run_id=run_id, owner_id=run.owner_id, role="assistant", content=final_text))
        await db.flush()
    return completed


async def complete_expired_run(db: AsyncSession, run_id: str, *, checkpoint_id: str, final_text: str) -> AgentRun:
    """仅凭已结束且归属明确的检查点，原子补写过期运行的最终回答。"""
    if not checkpoint_id or not final_text.strip() or len(final_text) > 262144:
        raise ValueError("无效的补偿检查点或最终回答")
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    completed = await _change_state(
        db, run, "completed", now=shanghai_now(naive=True), checkpoint_id=checkpoint_id, require_expired_lease=True,
    )
    db.add(AgentRunMessage(run_id=run_id, owner_id=run.owner_id, role="assistant", content=final_text))
    await db.flush()
    return completed


async def fail_run(db: AsyncSession, run_id: str, lease_token: str, failure_code: str) -> AgentRun:
    """由持有租约的执行器记录分类故障，不保存原始异常信息。"""
    if failure_code not in {"model_error", "empty_response", "checkpoint_error", "checkpoint_mismatch", "runtime_error"}:
        raise ValueError("不允许的运行故障类别")
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    return await _change_state(
        db, run, "failed", now=shanghai_now(naive=True), lease_token=lease_token,
        failure_code=failure_code, require_lease=True,
    )


async def timeout_run(db: AsyncSession, run_id: str, lease_token: str) -> AgentRun:
    """由持有租约的执行器在超时取消完成后标记终态。"""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    return await _change_state(
        db, run, "timed_out", now=shanghai_now(naive=True), lease_token=lease_token,
        failure_code="execution_timeout", require_lease=True,
    )


async def pause_for_approval(db: AsyncSession, run_id: str, lease_token: str, checkpoint_id: str) -> AgentRun:
    """保存检查点引用并释放执行器租约，等待审批。"""
    if not checkpoint_id:
        raise ValueError("审批前必须保存检查点")
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    return await _change_state(
        db, run, "waiting_for_approval", now=shanghai_now(naive=True), lease_token=lease_token,
        checkpoint_id=checkpoint_id, require_lease=True,
    )


async def create_approval_request(
    db: AsyncSession, run_id: str, lease_token: str, checkpoint_id: str, actions: list[dict], *, ttl_seconds: int = 3600,
) -> RunApproval:
    """在保存暂停状态的同一事务中记录不含原始工具参数的审批请求。"""
    if not actions or len(actions) > 20 or ttl_seconds <= 0:
        raise ValueError("无效的审批动作或到期时长")
    if any(
        set(action) != {"name", "arguments_hash"} or not isinstance(action["name"], str)
        or not 1 <= len(action["name"]) <= 128 or not isinstance(action["arguments_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", action["arguments_hash"]) is None
        for action in actions
    ):
        raise ValueError("审批动作只能包含工具名和参数摘要")
    run = await db.get(AgentRun, run_id)
    if run is None or run.state != "running" or run.lease_token != lease_token:
        raise RunConflictError("执行器未持有运行租约")
    await pause_for_approval(db, run_id, lease_token, checkpoint_id)
    approval = RunApproval(
        id=str(uuid4()), run_id=run_id, owner_id=run.owner_id, checkpoint_id=checkpoint_id,
        actions=actions, state="pending", expires_at=shanghai_now(naive=True) + timedelta(seconds=ttl_seconds),
    )
    db.add(approval)
    await db.flush()
    return approval


async def get_owned_approval(db: AsyncSession, run_id: str, owner_id: int) -> RunApproval:
    """读取当前用户所属运行的最新审批请求。"""
    await get_owned_run(db, run_id, owner_id)
    statement = select(RunApproval).where(RunApproval.run_id == run_id, RunApproval.owner_id == owner_id)
    approval = await db.scalar(statement.order_by(RunApproval.create_time.desc(), RunApproval.id.desc()).limit(1))
    if approval is None:
        raise RunNotFoundError("审批请求不存在")
    return approval


async def decide_approval(db: AsyncSession, run_id: str, owner_id: int, decision: str) -> RunApproval:
    """仅允许运行所有者决定一次审批；拒绝时同步结束运行。"""
    if decision not in {"approve", "reject"}:
        raise ValueError("无效的审批决定")
    run = await get_owned_run(db, run_id, owner_id)
    approval = await get_owned_approval(db, run_id, owner_id)
    now = shanghai_now(naive=True)
    if run.state != "waiting_for_approval" or run.checkpoint_id != approval.checkpoint_id or approval.state != "pending":
        raise RunConflictError("运行或审批状态已变化")
    if approval.expires_at <= now:
        raise RunConflictError("审批请求已过期")
    next_state = "approved" if decision == "approve" else "rejected"
    statement = update(RunApproval).where(
        RunApproval.id == approval.id, RunApproval.state == "pending", RunApproval.expires_at > now,
    ).values(state=next_state, decided_by=owner_id, decided_at=now)
    result = await db.execute(statement.execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise RunConflictError("审批请求已被处理")
    if decision == "reject":
        await _change_state(db, run, "cancelled", now=now, failure_code="approval_rejected")
    await db.refresh(approval)
    return approval


async def expire_approvals(db: AsyncSession, *, limit: int = 100) -> int:
    """将到期审批和仍在等待的关联运行一起转为终态。"""
    if not 1 <= limit <= 1000:
        raise ValueError("无效的审批回收数量")
    now = shanghai_now(naive=True)
    statement = select(RunApproval).where(
        RunApproval.state == "pending", RunApproval.expires_at <= now,
    ).order_by(RunApproval.expires_at, RunApproval.id).limit(limit)
    count = 0
    for approval in (await db.scalars(statement)).all():
        result = await db.execute(update(RunApproval).where(
            RunApproval.id == approval.id, RunApproval.state == "pending",
        ).values(state="expired").execution_options(synchronize_session=False))
        if result.rowcount != 1:
            continue
        run = await db.get(AgentRun, approval.run_id)
        if run is not None and run.state == "waiting_for_approval" and run.checkpoint_id == approval.checkpoint_id:
            await _change_state(db, run, "timed_out", now=now, failure_code="approval_expired")
        count += 1
    return count


async def resume_approved_run(db: AsyncSession, run_id: str, *, lease_seconds: int = 30) -> AgentRun:
    """仅在匹配当前检查点的审批已同意时分配新租约。"""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    approval = await db.scalar(select(RunApproval).where(
        RunApproval.run_id == run_id, RunApproval.checkpoint_id == run.checkpoint_id,
    ))
    if approval is None or approval.state != "approved":
        raise RunConflictError("审批尚未同意")
    return await _change_state(db, run, "running", now=shanghai_now(naive=True), lease_token=str(uuid4()), lease_seconds=lease_seconds)


async def request_cancel(db: AsyncSession, run_id: str, owner_id: int) -> AgentRun:
    """由所有者请求取消；排队或待审批任务直接结束，运行中任务等待执行器确认。"""
    run = await get_owned_run(db, run_id, owner_id)
    for _ in range(5):
        if run.state in TERMINAL_STATES or run.state == "cancel_requested":
            return run
        if run.state == "waiting_for_approval":
            await db.execute(update(RunApproval).where(
                RunApproval.run_id == run_id, RunApproval.state == "pending",
            ).values(state="cancelled").execution_options(synchronize_session=False))
        state = "cancel_requested" if run.state == "running" else "cancelled"
        try:
            return await _change_state(db, run, state, now=shanghai_now(naive=True))
        except RunConflictError:
            await db.refresh(run)
    raise RunConflictError("取消运行时状态持续变化")


async def acknowledge_cancel(db: AsyncSession, run_id: str, lease_token: str) -> AgentRun:
    """执行器确认已经停止后将取消请求改为终态。"""
    run = await db.get(AgentRun, run_id)
    if run is None:
        raise RunNotFoundError("运行不存在")
    return await _change_state(db, run, "cancelled", now=shanghai_now(naive=True), lease_token=lease_token, require_lease=True)


async def recover_expired_runs(db: AsyncSession, *, limit: int = 100) -> int:
    """回收过期租约；因副作用不可自动重放，将遗留运行标记失败或取消。"""
    if not 1 <= limit <= 1000:
        raise ValueError("无效的回收数量")
    now = shanghai_now(naive=True)
    statement = select(AgentRun).where(
        AgentRun.state.in_(("running", "cancel_requested")), AgentRun.lease_expires_at <= now,
    ).order_by(AgentRun.lease_expires_at, AgentRun.id).limit(limit)
    count = 0
    for run in (await db.scalars(statement)).all():
        try:
            state = "cancelled" if run.state == "cancel_requested" else "failed"
            await _change_state(db, run, state, now=now, failure_code="lease_expired")
            count += 1
        except RunConflictError:
            continue
    return count
