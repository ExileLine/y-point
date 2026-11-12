import { useEffect, useState } from "react";
import { Button, Tooltip } from "antd";
import { Clock3, Gauge, Layers3 } from "lucide-react";
import { ApiError, getSessionMetrics, type SessionMetrics as Metrics } from "../api";
import { elapsedLabel } from "./chat-format";

export default function SessionMetrics({ agentId, sessionId, refreshKey, active }: {
  agentId: number; sessionId: string; refreshKey: number; active: boolean;
}) {
  const [snapshot, setSnapshot] = useState<{ metrics: Metrics; receivedAt: number } | null>(null);
  const [now, setNow] = useState(performance.now());
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    let timer: number | undefined;
    const controller = new AbortController();
    async function load() {
      let denied = false;
      try {
        const metrics = await getSessionMetrics(agentId, sessionId, controller.signal);
        if (current) {
          const receivedAt = performance.now();
          setSnapshot({ metrics, receivedAt });
          setNow(receivedAt);
          setError("");
        }
      } catch (failure) {
        if (current) {
          denied = failure instanceof ApiError && [401, 403, 404].includes(failure.status);
          if (denied) setSnapshot(null);
          setError(denied ? "会话统计不可访问" : "会话统计暂时无法同步");
        }
      } finally {
        if (current && !denied && active) timer = window.setTimeout(() => void load(), 2500);
      }
    }
    void load();
    return () => { current = false; controller.abort(); window.clearTimeout(timer); };
  }, [agentId, sessionId, refreshKey, active, retry]);
  useEffect(() => {
    if (!active || !snapshot?.metrics.active_run_count || error) return;
    const timer = window.setInterval(() => setNow(performance.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, snapshot?.metrics.active_run_count, error]);
  const metrics = snapshot?.metrics;
  const elapsed = metrics ? metrics.elapsed_ms + (active && !error ? Math.max(0, now - snapshot!.receivedAt) * metrics.active_run_count : 0) : null;
  return <div className="session-totals" aria-label="整个会话统计">
    <span className="session-totals-title"><Layers3 size={14} aria-hidden="true" />会话累计{metrics && <span> · {metrics.run_count} 轮</span>}</span>
    {metrics ? <>
      <Tooltip title="累计各轮从提交到结束的耗时，包含排队、审批、失败与取消；排除轮次之间的空闲时间，不重复叠加工具耗时">
        <span><Clock3 size={13} aria-hidden="true" />总耗时 {metrics.missing_duration_runs > 0 ? "至少 " : ""}{elapsedLabel(elapsed!)}{active && !error && metrics.active_run_count > 0 ? " · 计时中" : ""}</span>
      </Tooltip>
      <Tooltip title="整个会话所有轮次模型调用与摘要的累计已报告 Token；上游未报告或失败请求的用量可能缺失，不等于供应商账单">
        <span><Gauge size={13} aria-hidden="true" />总用量 {metrics.total_tokens === null ? "Token 未报告" : `${metrics.total_tokens.toLocaleString("zh-CN")} Token · 已报告`}</span>
      </Tooltip>
      {metrics.missing_duration_runs > 0 && <span className="session-totals-notice">{metrics.missing_duration_runs} 轮历史耗时未知</span>}
    </> : !error && <span>正在统计…</span>}
    {error && <span className="session-totals-notice" role="status">{error}{metrics ? "，显示上次统计" : ""}<Button type="link" size="small" onClick={() => setRetry(value => value + 1)}>重试</Button></span>}
  </div>;
}
