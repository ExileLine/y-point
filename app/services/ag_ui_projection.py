# -*- coding: utf-8 -*-
# @Time    : 2026-11-17 10:05:22
# @Author  : yangyuexiong
# @File    : ag_ui_projection.py

"""从唯一的持久化领域事件源确定性投影 AG-UI 1.0；连接不拥有执行生命周期。"""

from datetime import timezone, timedelta
import json
from uuid import NAMESPACE_URL, uuid5

from ag_ui.core import Event
from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunCallFailure, RunEventType, RunState
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.services.run_call_failures import call_failure_message
from app.services.model_availability import MODEL_AVAILABILITY_MESSAGES

EVENT_ADAPTER = TypeAdapter(Event)


class RunProjection:
    """以有序事件驱动公开协议状态；重建与在线增量得到相同 ID、时间和线序号。"""

    def __init__(self, run: AgentRun, *, first_run_id: str | None = None) -> None:
        """只读取不可变运行身份；协议 attempt 与业务运行及检查点线程分别标识。"""
        self.business_id = run.id
        self.thread_id = run.session_id or run.id
        self.first_run_id = first_run_id or run.ag_ui_run_id or run.id
        self.attempt = 0
        self.run_id = self.first_run_id
        self.open = False
        self.started = False
        self.sequence = 0
        self.source_sequence = 0
        self.segment = "initial"
        self.text_id: str | None = None
        self.reasoning_id: str | None = None
        self.last_text_id: str | None = None
        self.last_text = ""
        self.tools: dict[str, dict] = {}
        self.timestamp = 0
        self.output: list[dict] = []

    def identity(self, name: str) -> str:
        """由运行与语义身份生成可重复的公开 ID。"""
        return str(uuid5(NAMESPACE_URL, f"y-point:ag-ui:1:{self.business_id}:{name}"))

    def emit(self, event_type: str, **fields) -> None:
        """使用锁定的官方 SDK 校验并编码封闭事件结构。"""
        event = EVENT_ADAPTER.validate_python(
            {
                "type": event_type,
                "timestamp": self.timestamp,
                **fields,
            },
        ).model_dump(mode="json", by_alias=True, exclude_none=True)
        self.sequence += 1
        self.output.append(
            {
                "sequence": self.sequence,
                "event": event,
            },
        )

    def start(self, protocol_run_id: str | None = None) -> None:
        """开启首轮或审批后的新协议运行，不把审批续跑标记为子 Agent。"""
        if self.open:
            return
        self.run_id = protocol_run_id or (self.first_run_id if not self.started else self.identity(f"attempt:{self.attempt}"))
        self.attempt += 1
        self.open = self.started = True
        self.emit(
            "RUN_STARTED",
            threadId=self.thread_id,
            runId=self.run_id,
            protocolVersion="1.0",
            metadata={
                "yPoint": {
                    "businessRunId": self.business_id,
                    "attempt": self.attempt,
                },
            },
        )

    def close_messages(self) -> None:
        """闭合当前公开思考与正文消息，但不推断工具执行已成功。"""
        if self.reasoning_id:
            self.emit("REASONING_MESSAGE_END", messageId=self.reasoning_id)
            self.emit("REASONING_END", messageId=self.reasoning_id)
            self.reasoning_id = None
        if self.text_id:
            self.emit("TEXT_MESSAGE_END", messageId=self.text_id)
            self.text_id = None

    def activity(
        self,
        call_id: str,
        name: str,
        status: str,
        *,
        failure_code: str | None = None,
        skill_phase: str | None = None,
        call_kind: str | None = None,
        display_name: str | None = None,
    ) -> None:
        """公开固定白名单摘要；不向浏览器复制工具原始参数、返回值或 Skill 正文。"""
        kind = "skill" if name.startswith("skill:") else "knowledge" if "knowledge" in name else "mcp" if "mcp" in name else "tool"
        previous = self.tools.get(call_id, {})
        kind = call_kind or previous.get("kind") or kind
        name = display_name or previous.get("name") or name
        content = {
            "version": 1,
            "toolCallId": call_id,
            "name": name,
            "kind": kind,
            "status": status,
            "startedAt": previous.get("startedAt", self.timestamp),
            "summary": "",
            "skillPhase": skill_phase or previous.get("skillPhase"),
        }
        if kind == "skill" and status == "succeeded":
            content["summary"] = (
                "已加载技能说明" if content["skillPhase"] == "instructions" else "技能脚本已完成" if content["skillPhase"] == "script" else "已读取技能资料"
            )
        if status != "running":
            content["finishedAt"] = self.timestamp
        if failure_code:
            content["failureCode"] = failure_code
            content["failureReason"] = call_failure_message(failure_code)
        self.tools[call_id] = content
        self.emit("ACTIVITY_SNAPSHOT", messageId=self.identity(f"activity:{call_id}"), activityType="y-point.call", content=content)

    def finish(self, outcome: dict) -> None:
        """结束协议运行，保留未完成工具为中断或结果未确认。"""
        self.close_messages()
        for call_id, tool in list(self.tools.items()):
            if tool["status"] == "running":
                self.activity(call_id, tool["name"], "interrupted")
        self.emit(
            "ACTIVITY_SNAPSHOT",
            messageId=self.identity("run-state"),
            activityType="y-point.run",
            content={
                "version": 1,
                "state": "waiting_for_approval" if outcome["type"] == "interrupt" else "cancelled" if outcome["type"] == "cancelled" else "completed",
            },
        )
        self.emit("RUN_FINISHED", threadId=self.thread_id, runId=self.run_id, outcome=outcome)
        self.open = False

    def feed(self, source: AgentRunEvent) -> list[dict]:
        """投影单个持久化事件，返回本次产生的完整协议帧。"""
        self.output = []
        self.source_sequence = source.sequence
        self.timestamp = int(source.create_time.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)
        details = source.details or {}
        kind = source.event_type
        if kind == RunEventType.STATE:
            if source.state in (RunState.QUEUED, RunState.RUNNING):
                self.start(details.get("protocol_run_id"))
                self.emit(
                    "ACTIVITY_SNAPSHOT",
                    messageId=self.identity("run-state"),
                    activityType="y-point.run",
                    content={
                        "version": 1,
                        "state": source.state,
                    },
                )
            elif self.open and source.state == RunState.WAITING_FOR_APPROVAL:
                self.finish(
                    {
                        "type": "interrupt",
                        "interrupts": [
                            {
                                "id": details.get("interrupt_id", self.identity(f"interrupt:{source.sequence}")),
                                "reason": "tool_approval",
                                "message": "工具调用需要你的确认",
                                "metadata": {
                                    "actions": details.get("actions", []),
                                    "expiresAt": details.get("expires_at"),
                                },
                            },
                        ],
                    },
                )
            elif source.state == RunState.CANCELLED and (self.open or details.get("protocol_run_id")):
                self.start(details.get("protocol_run_id"))
                self.finish(
                    {
                        "type": "cancelled",
                    },
                )
            elif self.started and source.state in (RunState.FAILED, RunState.TIMED_OUT):
                self.close_messages()
                for call_id, tool in list(self.tools.items()):
                    if tool["status"] == "running":
                        self.activity(call_id, tool["name"], "interrupted")
                self.emit("RUN_ERROR", message=MODEL_AVAILABILITY_MESSAGES.get(source.failure_code, "本轮运行未完成"), code=source.failure_code or source.state)
                self.open = False
            # completed 的状态事件先于模型产物提交，交付屏障才是真正的成功终点。
            return self.output
        if not self.open:
            return []
        if details.get("metrics"):
            self.emit(
                "CUSTOM",
                name="y-point.run.metrics",
                value={
                    "version": 1,
                    "businessRunId": self.business_id,
                    **details["metrics"],
                },
            )
        if kind == RunEventType.MODEL_STARTED:
            self.close_messages()
            self.segment = details["segment_id"]
            self.last_text = ""
            self.last_text_id = None
        elif kind == RunEventType.MODEL_FINISHED:
            self.close_messages()
        elif kind == RunEventType.REASONING_DELTA and source.content:
            if not self.reasoning_id:
                self.reasoning_id = self.identity(f"reasoning:{self.attempt}:{self.segment}:{source.sequence}")
                self.emit("REASONING_START", messageId=self.reasoning_id)
                self.emit("REASONING_MESSAGE_START", messageId=self.reasoning_id, role="reasoning")
            self.emit("REASONING_MESSAGE_CONTENT", messageId=self.reasoning_id, delta=source.content)
        elif kind == RunEventType.TEXT_DELTA and source.content:
            if not self.text_id:
                self.text_id = self.identity(f"text:{self.attempt}:{self.segment}:{source.sequence}")
                self.last_text_id = self.text_id
                self.last_text = ""
                self.emit("TEXT_MESSAGE_START", messageId=self.text_id, role="assistant")
            self.last_text += source.content
            self.emit("TEXT_MESSAGE_CONTENT", messageId=self.text_id, delta=source.content)
        elif kind == RunEventType.TOOL_STARTED:
            self.close_messages()
            call_id = source.tool_call_id
            parent_id = self.identity(f"tool-parent:{call_id}")
            self.emit("TEXT_MESSAGE_START", messageId=parent_id, role="assistant")
            self.emit("TOOL_CALL_START", toolCallId=call_id, toolCallName=source.tool_name, parentMessageId=parent_id)
            self.emit("TOOL_CALL_ARGS", toolCallId=call_id, delta='{"displayOnly":true,"arguments":"redacted"}')
            self.emit("TOOL_CALL_END", toolCallId=call_id)
            self.emit("TEXT_MESSAGE_END", messageId=parent_id)
            self.activity(
                call_id,
                source.tool_name,
                "running",
                skill_phase=details.get("skill_phase"),
                call_kind=details.get("call_kind"),
                display_name=details.get("display_name"),
            )
        elif kind in (RunEventType.TOOL_COMPLETED, RunEventType.TOOL_FAILED):
            status = "succeeded" if kind == RunEventType.TOOL_COMPLETED else "interrupted" if source.failure_code == RunCallFailure.RESULT_UNKNOWN else "failed"
            result = {
                "displayOnly": True,
                "status": status,
                "failureCode": source.failure_code,
            }
            self.emit(
                "TOOL_CALL_RESULT",
                messageId=self.identity(f"result:{source.tool_call_id}"),
                toolCallId=source.tool_call_id,
                content=json.dumps(result, ensure_ascii=False),
            )
            self.activity(source.tool_call_id, source.tool_name, status, failure_code=source.failure_code, skill_phase=details.get("skill_phase"))
        elif kind == RunEventType.ARTIFACT_PUBLISHED:
            self.emit(
                "CUSTOM",
                name="y-point.artifact.available",
                value={
                    "version": 1,
                    "artifactId": source.artifact_id,
                },
            )
        elif kind == RunEventType.DELIVERY_COMPLETED:
            self.close_messages()
            final_text = details.get("final_text")
            if final_text and final_text != self.last_text:
                self.last_text_id = self.identity("final-answer")
                self.emit("TEXT_MESSAGE_START", messageId=self.last_text_id, role="assistant")
                self.emit("TEXT_MESSAGE_CONTENT", messageId=self.last_text_id, delta=final_text)
                self.emit("TEXT_MESSAGE_END", messageId=self.last_text_id)
            self.emit(
                "CUSTOM",
                name="y-point.answer",
                value={
                    "version": 1,
                    "messageId": self.last_text_id,
                },
            )
            self.finish(
                {
                    "type": "success",
                },
            )
        return self.output


async def load_projection(db: AsyncSession, run: AgentRun, *, first_run_id: str | None = None, through: int | None = None) -> tuple[RunProjection, list[dict]]:
    """固定源事件上界重建投影；线游标只属于版本化的 Y-Point 恢复扩展。"""
    projection = RunProjection(run, first_run_id=first_run_id)
    events = await db.scalars(
        select(AgentRunEvent)
        .where(AgentRunEvent.run_id == run.id, AgentRunEvent.sequence <= (run.event_seq if through is None else through))
        .order_by(AgentRunEvent.sequence),
    )
    frames = []
    for source in events:
        frames.extend(projection.feed(source))
    return projection, frames


def projection_messages(frames: list[dict]) -> list[dict]:
    """聚合标准协议产生的全部公开消息，供通用客户端提交完整历史而非只提交最后一问。"""
    from ag_ui.core import Message

    message_adapter = TypeAdapter(Message)
    messages: dict[str, dict] = {}
    calls: dict[str, dict] = {}
    for frame in frames:
        event = frame["event"]
        kind = event["type"]
        message_id = event.get("messageId")
        if kind in ("TEXT_MESSAGE_START", "REASONING_MESSAGE_START"):
            messages[message_id] = {
                "id": message_id,
                "role": "reasoning" if kind == "REASONING_MESSAGE_START" else "assistant",
                "content": "",
            }
        elif kind in ("TEXT_MESSAGE_CONTENT", "REASONING_MESSAGE_CONTENT"):
            messages[message_id]["content"] += event["delta"]
        elif kind == "TOOL_CALL_START":
            call = {
                "id": event["toolCallId"],
                "type": "function",
                "function": {
                    "name": event["toolCallName"],
                    "arguments": "",
                },
            }
            calls[event["toolCallId"]] = call
            messages[event["parentMessageId"]].setdefault("toolCalls", []).append(call)
        elif kind == "TOOL_CALL_ARGS":
            calls[event["toolCallId"]]["function"]["arguments"] += event["delta"]
        elif kind == "TOOL_CALL_RESULT":
            messages[message_id] = {
                "id": message_id,
                "role": "tool",
                "toolCallId": event["toolCallId"],
                "content": event["content"],
            }
        elif kind == "ACTIVITY_SNAPSHOT":
            messages[message_id] = {
                "id": message_id,
                "role": "activity",
                "activityType": event["activityType"],
                "content": event["content"],
            }
    return [message_adapter.validate_python(message).model_dump(mode="json", by_alias=True, exclude_none=True) for message in messages.values()]
