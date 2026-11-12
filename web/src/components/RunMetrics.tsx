import { useEffect, useState } from "react";
import { Tooltip } from "antd";
import { Clock3, Gauge } from "lucide-react";
import type { RunMetrics as Metrics, SessionRun } from "../api";
import { elapsedLabel, serverTime } from "./chat-format";

export default function RunMetrics({ metrics, run, active = false }: { metrics?: Metrics | null; run?: SessionRun | null; active?: boolean }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, run?.id]);
  const elapsed = metrics?.elapsed_ms ?? (active && run ? Math.max(0, now - serverTime(run.create_time)) : null);
  const tokens = metrics?.total_tokens ?? ((run?.reported_total_tokens || 0) > 0 ? run!.reported_total_tokens : null);
  if (!metrics && !run) return null;
  return <div className="run-metrics" aria-label="本轮统计">
    <Tooltip title="从提交到结束的总耗时，包含排队、模型生成、工具调用和审批等待"><span><Clock3 size={13} aria-hidden="true" />{active ? "已用时" : "本轮耗时"} {elapsed !== null && Number.isFinite(elapsed) ? elapsedLabel(elapsed) : "未知"}</span></Tooltip>
    <Tooltip title="本轮所有模型调用和摘要累计已报告的 Token；上游未报告或失败请求的用量可能缺失，不等于供应商账单"><span><Gauge size={13} aria-hidden="true" />{tokens !== null && tokens !== undefined ? `${tokens.toLocaleString("zh-CN")} Token · 已报告` : "Token 未报告"}</span></Tooltip>
  </div>;
}
