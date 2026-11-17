import { useEffect, useRef, useState } from "react";
import { Alert, Button, Space, Spin } from "antd";
import { getRun, getRunApproval, type RunApproval as Approval, type SessionRun } from "../api";
import { serverTime } from "./chat-format";

const stateLabels: Record<Approval["state"], string> = {
  pending: "请确认本次工具调用",
  approved: "已同意，等待执行器继续运行",
  rejected: "已拒绝，本轮运行已取消",
  expired: "审批已过期，请重新发起请求",
  cancelled: "审批已取消",
};

export default function RunApproval({ runId, onResume, onRunChanged }: {
  runId: string; onResume: (approvalId: string, decision: "approve" | "reject") => Promise<void>;
  onRunChanged: (run: SessionRun) => void;
}) {
  const [approval, setApproval] = useState<Approval | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [decisionError, setDecisionError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [revision, setRevision] = useState(0);
  const inFlight = useRef(false);
  const mounted = useRef(false);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  useEffect(() => {
    if (submitting) return;
    const controller = new AbortController();
    let timer: number | undefined;
    async function load() {
      try {
        const current = await getRunApproval(runId, controller.signal);
        if (controller.signal.aborted) return;
        setApproval(current);
        setLoadError("");
      } catch (failure) {
        if (controller.signal.aborted) return;
        setApproval(null);
        setLoadError(failure instanceof Error ? failure.message : "审批请求加载失败");
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = window.setTimeout(() => void load(), 3000);
        }
      }
    }
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [runId, revision, submitting]);

  async function decide(decision: "approve" | "reject") {
    if (!approval || approval.state !== "pending" || inFlight.current) return;
    inFlight.current = true;
    setSubmitting(true);
    setDecisionError("");
    try {
      await onResume(approval.id, decision);
    } catch (failure) {
      if (mounted.current) setDecisionError(failure instanceof Error ? failure.message : "审批提交失败，请核对最新状态后重试");
    }
    try {
      const current = await getRun(runId);
      if (mounted.current) onRunChanged(current);
    } catch (failure) {
      if (mounted.current) setDecisionError(failure instanceof Error ? failure.message : "运行状态刷新失败");
    } finally {
      inFlight.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  return <section className="run-approval" aria-label="工具审批">
    {loading && !approval && <Spin size="small" aria-label="加载审批" />}
    {loadError && <Alert type="error" showIcon message={loadError} action={<Button size="small" onClick={() => { setLoading(true); setRevision(value => value + 1); }}>重新加载审批</Button>} />}
    {decisionError && <Alert type="error" showIcon message={decisionError} />}
    {approval && <>
      <Alert type={approval.state === "pending" || approval.state === "expired" ? "warning" : "info"} showIcon message={stateLabels[approval.state]}
        description={approval.state === "pending" ? "同意后执行以下工具；拒绝会取消本轮运行。连续调用可能需要分别确认。修改工具审批设置仅对下一轮生效；如已关闭，请取消本轮后重新发送。" : undefined} />
      <ul className="run-approval-actions">{approval.actions.map((action, index) => <li key={`${action.name}-${index}`}>
        <strong>{action.name}</strong>
        {action.resource_name && <span>目标文件：{action.resource_name}</span>}
        <details><summary>查看参数摘要</summary><code>{action.arguments_hash}</code></details>
      </li>)}</ul>
      <p className="run-approval-expiry">审批截止：{new Date(serverTime(approval.expires_at)).toLocaleString("zh-CN", { hour12: false })}</p>
      {approval.state === "pending" && !loadError && <Space wrap>
        <Button type="primary" aria-label="同意执行" loading={submitting} disabled={submitting} onClick={() => void decide("approve")}>同意执行</Button>
        <Button danger aria-label="拒绝执行" disabled={submitting} onClick={() => void decide("reject")}>拒绝执行</Button>
      </Space>}
    </>}
  </section>;
}
