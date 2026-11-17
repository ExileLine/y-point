import type { Event } from "@ag-ui/core";
import { EventSchema } from "@ag-ui/core/schemas";
import { createParser } from "eventsource-parser";
import { ApiError, getAgUiSnapshot, issueRunStreamTicket, runStreamUrl, type AgUiStream, type SessionRun } from "./api";

export type CallActivity = {
  version: number; toolCallId: string; name: string; kind: "skill" | "knowledge" | "mcp" | "tool";
  status: "running" | "succeeded" | "failed" | "interrupted"; startedAt: number;
  finishedAt?: number; failureCode?: string; failureReason?: string; summary?: string;
};
export type ProcessEntry =
  | { id: string; kind: "reasoning"; text: string; active: boolean }
  | { id: string; kind: "text"; text: string; active: boolean }
  | { id: string; kind: "call"; call: CallActivity };
export type ProcessState = {
  cursor: number; entries: ProcessEntry[]; protocolRunId: string | null;
  status: "queued" | "running" | "interrupted" | "succeeded" | "cancelled" | "failed";
  answerId: string | null; previewId: string | null; errorCode?: string;
};
export type AgUiFrame = { sequence: number; event: Event };
export const emptyProcess = (): ProcessState => ({
  cursor: 0, entries: [], protocolRunId: null, status: "queued", answerId: null, previewId: null,
});

/** Consume the supported AG-UI 1.0 subset without treating TOOL_CALL_END as execution success. */
export function reduceProcess(state: ProcessState, frame: AgUiFrame): ProcessState {
  if (frame.sequence <= state.cursor) return state;
  if (frame.sequence !== state.cursor + 1) throw new Error("过程事件不连续，正在从快照恢复");
  const event = frame.event;
  if (["succeeded", "cancelled", "failed", "interrupted"].includes(state.status)
    && event.type !== "RUN_STARTED" && event.type !== "RUN_ERROR") return { ...state, cursor: frame.sequence };
  const next = { ...state, cursor: frame.sequence, entries: [...state.entries] };
  if (event.type === "RUN_STARTED") {
    next.protocolRunId = event.runId;
    next.status = "running";
  } else if (event.type === "RUN_ERROR") {
    next.status = "failed";
    next.errorCode = event.code;
  } else if (event.type === "RUN_FINISHED") {
    next.status = state.status === "failed" ? "failed" : event.outcome?.type === "interrupt" ? "interrupted"
      : event.outcome?.type === "cancelled" ? "cancelled" : "succeeded";
  } else if ((event.type === "TEXT_MESSAGE_START" || event.type === "REASONING_MESSAGE_START")) {
    if (!next.entries.some(entry => entry.id === event.messageId)) next.entries.push({
      id: event.messageId, kind: event.type === "TEXT_MESSAGE_START" ? "text" : "reasoning", text: "", active: true,
    });
    if (event.type === "TEXT_MESSAGE_START") next.previewId = event.messageId;
  } else if (event.type === "TEXT_MESSAGE_CONTENT" || event.type === "REASONING_MESSAGE_CONTENT") {
    next.entries = next.entries.map(entry => entry.id === event.messageId && entry.kind !== "call"
      ? { ...entry, text: entry.text + event.delta } : entry);
  } else if (event.type === "TEXT_MESSAGE_END" || event.type === "REASONING_MESSAGE_END") {
    next.entries = next.entries.map(entry => entry.id === event.messageId && entry.kind !== "call" ? { ...entry, active: false } : entry);
  } else if (event.type === "TOOL_CALL_START") {
    next.previewId = null;
  } else if (event.type === "ACTIVITY_SNAPSHOT" && event.activityType === "y-point.call") {
    const call = event.content as unknown as CallActivity;
    if (call.version !== 1 || typeof call.toolCallId !== "string" || typeof call.name !== "string"
      || !["running", "succeeded", "failed", "interrupted"].includes(call.status)
      || typeof call.startedAt !== "number" || !Number.isFinite(call.startedAt)
      || !["skill", "knowledge", "mcp", "tool"].includes(call.kind)) return next;
    const existing = next.entries.findIndex(entry => entry.id === event.messageId);
    const entry: ProcessEntry = { id: event.messageId, kind: "call", call };
    if (existing < 0) next.entries.push(entry);
    else next.entries[existing] = entry;
  } else if (event.type === "CUSTOM" && event.name === "y-point.answer") {
    const value = event.value as { messageId?: string };
    next.answerId = value.messageId || null;
    next.previewId = next.answerId;
  }
  if (["succeeded", "cancelled", "failed", "interrupted"].includes(next.status)) {
    next.entries = next.entries.map(entry => entry.kind !== "call" ? { ...entry, active: false } : entry);
  }
  return next;
}

export function processAnswer(state: ProcessState): string {
  const entry = state.entries.find(item => item.id === (state.answerId || state.previewId));
  return entry && entry.kind === "text" ? entry.text : "";
}

/** Durable replay is a Y-Point extension. A disconnected observer never cancels the worker. */
export function observeProcess(runId: string, callbacks: {
  onFrame: (frame: AgUiFrame) => void; onRun: (run: SessionRun) => void;
  onConnection?: (fallback: boolean) => void; onResource?: () => void; onDenied?: () => void;
}, live = true, resourcesOnly = false, initial?: AgUiStream): () => void {
  let active = true;
  let cursor = 0;
  let stream: EventSource | null = null;
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  let timer: number | undefined;
  const controller = new AbortController();
  const apply = (frame: AgUiFrame) => {
    if (!active || frame.sequence <= cursor) return;
    if (frame.sequence !== cursor + 1) throw new Error("AG-UI cursor gap");
    callbacks.onFrame(frame);
    cursor = frame.sequence;
  };
  const readFrame = (id: string, data: string): AgUiFrame => {
    const prefix = `${runId}:`;
    if (!id.startsWith(prefix)) throw new Error("AG-UI event identity mismatch");
    const value = id.slice(prefix.length);
    if (!/^[1-9]\d*$/.test(value)) throw new Error("AG-UI invalid cursor");
    const sequence = Number(value);
    if (!Number.isSafeInteger(sequence)) throw new Error("AG-UI invalid cursor");
    return { sequence, event: EventSchema.parse(JSON.parse(data)) };
  };
  const retry = () => {
    stream?.close();
    stream = null;
    if (active) {
      callbacks.onConnection?.(true);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void connect(), 1500);
    }
  };
  async function connect() {
    try {
      let through: number | undefined;
      let snapshot;
      do {
        snapshot = await getAgUiSnapshot(runId, cursor, through, controller.signal);
        if (!active) return;
        through = snapshot.through;
        snapshot.events.forEach(apply);
      } while (snapshot.has_more);
      callbacks.onRun(snapshot.run);
      if (resourcesOnly && snapshot.resources_complete) { callbacks.onResource?.(); return; }
      if (!live || !active) return;
      await issueRunStreamTicket(runId);
      if (!active) return;
      stream = new EventSource(runStreamUrl(runId, cursor));
      stream.onopen = () => { if (active) callbacks.onConnection?.(false); };
      stream.onmessage = message => {
        if (!active) return;
        try {
          apply(readFrame(message.lastEventId, message.data));
        } catch { retry(); }
      };
      stream.addEventListener("y-point.run-state", message => {
        if (!active) return;
        try { callbacks.onRun(JSON.parse((message as MessageEvent).data) as SessionRun); } catch { retry(); }
      });
      stream.addEventListener("y-point.resource", () => {
        if (!active) return;
        callbacks.onResource?.();
        if (resourcesOnly) { active = false; stream?.close(); }
      });
      stream.onerror = retry;
    } catch (error) {
      if (!active) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) {
        callbacks.onDenied?.();
        active = false;
        return;
      }
      retry();
    }
  }
  async function consumePost(connection: AgUiStream) {
    try {
      // A resumed attempt starts after its durable interrupt. Rebuild that prefix first.
      if (connection.after > 0) {
        let through: number | undefined;
        do {
          const snapshot = await getAgUiSnapshot(runId, cursor, through, controller.signal);
          if (!active) return;
          through = snapshot.through;
          snapshot.events.filter(frame => frame.sequence <= connection.after).forEach(apply);
          if (!snapshot.has_more && cursor < connection.after) throw new Error("审批恢复游标缺少历史事件");
        } while (cursor < connection.after);
      }
      if (!active) return;
      callbacks.onConnection?.(false);
      let finished = false;
      const parser = createParser({ onEvent: message => {
        if (!active || finished) return;
        if (message.event && message.event !== "message") throw new Error("标准流包含非协议通知");
        const frame = readFrame(message.id || "", message.data);
        apply(frame);
        finished = frame.event.type === "RUN_FINISHED" || frame.event.type === "RUN_ERROR";
      } });
      reader = connection.response.body!.getReader();
      const decoder = new TextDecoder();
      while (active && !finished) {
        const chunk = await reader.read();
        if (chunk.done) { parser.feed(decoder.decode()); break; }
        parser.feed(decoder.decode(chunk.value, { stream: true }));
      }
      if (active && !finished) retry();
    } catch {
      if (active) retry();
    } finally {
      await reader?.cancel().catch(() => undefined);
      if (!connection.response.body?.locked) await connection.response.body?.cancel().catch(() => undefined);
    }
  }
  if (initial) void consumePost(initial);
  else void connect();
  return () => {
    active = false; controller.abort(); stream?.close(); window.clearTimeout(timer);
    void reader?.cancel().catch(() => undefined);
    if (initial && !initial.response.body?.locked) void initial.response.body?.cancel().catch(() => undefined);
  };
}
