import { modelAccessFailures } from "../model-failures";
import { useEffect, useId, useState } from "react";
import { Alert, Button } from "antd";
import { Activity, CheckCircle2, ChevronDown, CircleDashed, CircleHelp, XCircle } from "lucide-react";
import { emptyProcess, observeProcess, type ProcessState, reduceProcess } from "../ag-ui";
import ReasoningPanel from "./ReasoningPanel";
import RunCalls from "./RunCalls";
import { MessageContent } from "./MessageContent";

export default function RunActivity({ state, active = false, onProgress }: {
  state: ProcessState; active?: boolean; onProgress?: () => void;
}) {
  const [choice, setChoice] = useState<boolean | null>(null);
  const id = useId();
  const attention = state.status === "interrupted" || state.status === "failed"
    || state.entries.some(entry => entry.kind === "call" && ["failed", "interrupted"].includes(entry.call.status));
  const expanded = choice ?? (active || attention);
  const entries = state.entries.filter(entry => entry.id !== (state.answerId || state.previewId) && (entry.kind === "call" || Boolean(entry.text)));
  const calls = entries.filter(entry => entry.kind === "call");
  const completed = calls.filter(entry => entry.kind === "call" && entry.call.status === "succeeded").length;
  const running = [...calls].reverse().find(entry => entry.kind === "call" && entry.call.status === "running");
  const reasoning = entries.some(entry => entry.kind === "reasoning" && entry.active);
  const label = state.status === "interrupted" ? "等待工具审批" : state.status === "failed" ? "本轮运行未完成"
    : state.status === "cancelled" ? "本轮已取消" : !active ? "处理过程"
    : running?.kind === "call" ? `正在执行 ${running.call.name}` : reasoning ? "正在生成公开思考" : "正在处理";
  const Icon = state.status === "failed" ? XCircle : state.status === "cancelled" || state.status === "interrupted" ? CircleHelp
    : active ? CircleDashed : CheckCircle2;
  useEffect(() => { onProgress?.(); }, [state.cursor, expanded, onProgress]);
  if (!entries.length && !active && !attention) return null;
  return <section className="run-activity" aria-label="Agent 处理过程" data-process-status={state.status}>
    <button className="run-activity-toggle" type="button" aria-expanded={expanded} aria-controls={id} onClick={() => setChoice(!expanded)}>
      <Icon size={16} aria-hidden="true" /><span className="run-activity-title">{label}</span>
      {completed > 0 && <span className="run-activity-count">已完成 {completed} 项活动</span>}
      <ChevronDown size={15} aria-hidden="true" />
    </button>
    <div id={id} hidden={!expanded} className="run-activity-body">
      {state.errorCode && modelAccessFailures[state.errorCode] && <Alert type="error" showIcon message="模型暂不可用" description={modelAccessFailures[state.errorCode]} />}
      <ol className="run-activity-timeline">{entries.map(entry => <li key={entry.id}>
        {entry.kind === "reasoning" ? <><span className="run-activity-source">模型公开思考</span><ReasoningPanel text={entry.text} active={active && entry.active} /></>
          : entry.kind === "text" ? <details className="run-activity-text"><summary>中间说明</summary><MessageContent text={entry.text} /></details>
          : <><RunCalls calls={[{
            call_id: entry.call.toolCallId, kind: entry.call.kind, name: entry.call.name,
            status: entry.call.status, sequence: 0, started_at: new Date(entry.call.startedAt).toISOString(),
            finished_at: entry.call.finishedAt ? new Date(entry.call.finishedAt).toISOString() : null,
            elapsed_ms: entry.call.finishedAt ? Math.max(0, entry.call.finishedAt - entry.call.startedAt) : null,
            failure_code: entry.call.failureCode,
            failure_reason: entry.call.failureReason || (entry.call.failureCode ? `执行未成功（${entry.call.failureCode}），请检查对应工具配置或输入。` : undefined),
          }]} />{entry.call.summary && <p className="run-activity-summary">{entry.call.summary}</p>}</>}
      </li>)}</ol>
      {!entries.length && !state.errorCode && <p className="run-activity-empty"><Activity size={14} aria-hidden="true" />{state.status === "failed" ? "本轮执行失败" : "等待执行器返回活动"}</p>}
    </div>
  </section>;
}

export function HistoricalRunActivity({ runId }: { runId: string }) {
  const [state, setState] = useState(emptyProcess);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    setState(emptyProcess());
    setError(false);
    return observeProcess(runId, {
      onFrame: frame => setState(current => reduceProcess(current, frame)),
      onRun: run => setState(current => ({
        ...current,
        errorCode: run.failure_code || current.errorCode,
        status: run.state === "completed" ? "succeeded" : run.state === "cancelled" ? "cancelled"
          : ["failed", "timed_out"].includes(run.state) ? "failed" : current.status,
      })),
      onConnection: fallback => setError(fallback),
      onDenied: () => { setState(emptyProcess()); setError(true); },
    }, false);
  }, [runId, revision]);
  return error ? <Alert type="warning" showIcon message="处理过程暂时无法读取" action={<Button size="small" onClick={() => setRevision(value => value + 1)}>重试</Button>} />
    : <RunActivity state={state} />;
}
