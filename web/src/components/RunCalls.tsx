import { useEffect, useState } from "react";
import { Button, Tooltip } from "antd";
import { BookOpen, CheckCircle2, CircleDashed, CircleHelp, Puzzle, Wrench, XCircle } from "lucide-react";
import { ApiError, getRunCalls, type RunCall } from "../api";

const kinds = { skill: { label: "Skill", Icon: Puzzle }, tool: { label: "Tool", Icon: Wrench }, knowledge: { label: "知识库", Icon: BookOpen } };
const states = {
  running: { label: "执行中", Icon: CircleDashed },
  succeeded: { label: "成功", Icon: CheckCircle2 },
  failed: { label: "失败", Icon: XCircle },
  interrupted: { label: "结果未确认", Icon: CircleHelp },
};

export default function RunCalls({ calls }: { calls: RunCall[] }) {
  if (!calls.length) return null;
  return <section className="run-calls" aria-label="本轮调用">
    <span className="run-calls-heading">本轮调用 · {calls.length}</span>
    <ul className="run-calls-list">{calls.map(call => {
      const { label, Icon } = kinds[call.kind] || kinds.tool;
      const state = states[call.status] || states.interrupted;
      return <li className="run-call" key={call.call_id} data-call-id={call.call_id}>
        <Icon className="run-call-kind-icon" size={14} aria-hidden="true" />
        <span className="run-call-kind">{label}</span>
        <span className="run-call-name">{call.name}</span>
        <Tooltip title={call.status === "interrupted" ? "未收到调用完成事件，无法确认执行结果" : undefined}>
          <span className={`run-call-result run-call-${call.status}`}><state.Icon size={13} aria-hidden="true" />{state.label}</span>
        </Tooltip>
      </li>;
    })}</ul>
  </section>;
}

export function ActiveRunCalls({ runId, refreshKey, active }: { runId: string; refreshKey: number; active: boolean }) {
  const [calls, setCalls] = useState<RunCall[]>([]);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    let timer: number;
    const controller = new AbortController();
    async function load() {
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
        if (current && active && !denied) timer = window.setTimeout(() => {
          if (document.visibilityState === "visible") void load();
          else timer = window.setTimeout(() => void load(), 2500);
        }, 2500);
      }
    }
    // 合并短时间内连续到达的工具事件，定时重读用于补齐断线期间或未收到完成通知的状态。
    timer = window.setTimeout(() => void load(), 120);
    return () => { current = false; window.clearTimeout(timer); controller.abort(); };
  }, [runId, refreshKey, active, retry]);
  return <>
    <RunCalls calls={calls} />
    {error && <div className="run-calls-error" role="status">{error}<Button type="link" size="small" onClick={() => setRetry(value => value + 1)}>重试</Button></div>}
  </>;
}
