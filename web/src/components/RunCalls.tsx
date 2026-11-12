import { useEffect, useRef, useState } from "react";
import { Button, Tooltip } from "antd";
import { BookOpen, CheckCircle2, CircleDashed, CircleHelp, Puzzle, PlugZap, Wrench, XCircle } from "lucide-react";
import { ApiError, getRunCalls, type RunCall } from "../api";

import { elapsedLabel, serverTime } from "./chat-format";

const kinds = { mcp: { label: "MCP", Icon: PlugZap }, skill: { label: "Skill", Icon: Puzzle }, tool: { label: "Tool", Icon: Wrench }, knowledge: { label: "知识库", Icon: BookOpen } };
const states = {
  running: { label: "执行中", Icon: CircleDashed },
  succeeded: { label: "成功", Icon: CheckCircle2 },
  failed: { label: "失败", Icon: XCircle },
  interrupted: { label: "结果未确认", Icon: CircleHelp },
};

function CallDuration({ call }: { call: RunCall }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (call.status !== "running" || !call.started_at) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [call.status, call.started_at]);
  const elapsed = call.elapsed_ms ?? (call.status === "running" && call.started_at ? Math.max(0, now - serverTime(call.started_at)) : null);
  return <span className="run-call-duration">{elapsed !== null && Number.isFinite(elapsed) ? elapsedLabel(elapsed) : "耗时未知"}</span>;
}

export default function RunCalls({ calls }: { calls: RunCall[] }) {
  if (!calls.length) return null;
  return <section className="run-calls" aria-label="本轮调用">
    <span className="run-calls-heading">本轮调用 · {calls.length}</span>
    <ul className="run-calls-list">{calls.map(call => {
      const { label, Icon } = kinds[call.kind] || kinds.tool;
      const state = states[call.status] || states.interrupted;
      return <li className={`run-call${call.status === "failed" ? " run-call-has-failure" : ""}`} key={call.call_id} data-call-id={call.call_id}>
        <Icon className="run-call-kind-icon" size={14} aria-hidden="true" />
        <span className="run-call-kind">{label}</span>
        <span className="run-call-name">{call.name}</span>
        <CallDuration call={call} />
        <Tooltip title={call.status === "interrupted" ? "未收到调用完成事件，无法确认执行结果" : undefined}>
          <span className={`run-call-result run-call-${call.status}`}><state.Icon size={13} aria-hidden="true" />{state.label}</span>
        </Tooltip>
        {(call.status === "failed" || call.status === "interrupted") && <details className="run-call-details">
          <summary>{call.status === "failed" ? "查看失败原因" : "查看中断说明"}</summary>
          <div className="run-call-explanation">
            <p>{call.status === "failed" ? (call.failure_reason || "本次调用失败，历史记录未保存具体原因。") : "未收到调用结束事件，无法确认执行结果。如涉及外部写入，请先核实结果再重新提交。"}</p>
            {call.status === "failed" && call.failure_code && <span className="run-call-error-code">错误码：{call.failure_code}</span>}
          </div>
        </details>}
      </li>;
    })}</ul>
  </section>;
}

export function ActiveRunCalls({ runId, refreshKey, active }: { runId: string; refreshKey: number; active: boolean }) {
  const [calls, setCalls] = useState<RunCall[]>([]);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const refresh = useRef<((polling: boolean) => void) | null>(null);
  useEffect(() => {
    let current = true;
    let polling = false;
    let inFlight = false;
    let queued = false;
    let timer: number | undefined;
    const controller = new AbortController();
    setCalls([]);
    setError("");

    function schedule(delay = 120) {
      window.clearTimeout(timer);
      if (inFlight) { queued = true; return; }
      timer = window.setTimeout(() => void load(), delay);
    }
    function poll() {
      timer = window.setTimeout(() => {
        if (!current || !polling) return;
        if (document.visibilityState === "visible") void load();
        else poll();
      }, 2500);
    }
    async function load() {
      if (!current) return;
      inFlight = true;
      queued = false;
      let denied = false;
      try {
        const rows = await getRunCalls(runId, controller.signal);
        if (current) { setCalls(rows); setError(""); }
      } catch (failure) {
        if (current) {
          denied = failure instanceof ApiError && [401, 403, 404].includes(failure.status);
          if (denied) setCalls([]);
          setError(denied ? "调用记录不可访问" : "调用状态暂时无法同步");
        }
      } finally {
        inFlight = false;
        if (current && !denied) {
          if (queued) schedule();
          else if (polling) poll();
        }
      }
    }
    // 同一 Run 的刷新顺序执行；在途期间的工具事件合并为随后的一次补读。
    refresh.current = shouldPoll => { polling = shouldPoll; schedule(); };
    return () => {
      current = false;
      refresh.current = null;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [runId]);
  useEffect(() => {
    refresh.current?.(active);
  }, [runId, refreshKey, active, retry]);
  return <>
    <RunCalls calls={calls} />
    {error && <div className="run-calls-error" role="status">{error}<Button type="link" size="small" onClick={() => setRetry(value => value + 1)}>重试</Button></div>}
  </>;
}
