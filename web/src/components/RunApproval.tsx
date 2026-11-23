import { useEffect, useRef, useState } from "react";
import { Alert, Button, Radio, Space, Spin } from "antd";
import { getRun, getRunApproval, type RunApproval as Approval, type SessionRun, type ApprovalDecision, type ApprovalActionDecision } from "../api";
import { serverTime } from "./chat-format";

const stateLabels: Record<Approval["state"], string> = {
  pending: "请确认本次工具调用",
  approved: "已同意，等待执行器继续运行",
  rejected: "已拒绝，本轮运行已取消",
  expired: "审批已过期，请重新发起请求",
  cancelled: "审批已取消",
  resolved: "已记录逐项决定，等待执行器继续整理结果",
};

export default function RunApproval({ runId, onResume, onRunChanged }: {
  runId: string; onResume: (approvalId: string, decision: ApprovalDecision, decisions?: ApprovalActionDecision[]) => Promise<void>;
  onRunChanged: (run: SessionRun) => void;
}) {
  const [approval, setApproval] = useState<Approval | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [decisionError, setDecisionError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [revision, setRevision] = useState(0);
  const [choices, setChoices] = useState<Record<string, "approve" | "reject">>({});
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

  async function decide(decision: ApprovalDecision) {
    if (!approval || approval.state !== "pending" || inFlight.current) return;
    if (decision === "resolve" && approval.actions.some(action => !action.action_id || !choices[`${approval.id}:${action.action_id}`])) {
      setDecisionError("请逐项选择批准或拒绝，再提交决定");
      return;
    }
    inFlight.current = true;
    setSubmitting(true);
    setDecisionError("");
    try {
      await onResume(approval.id, decision, decision === "resolve" ? approval.actions.map(action => ({ action_id: action.action_id!, decision: choices[`${approval.id}:${action.action_id}`] })) : undefined);
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
        description={approval.state === "pending" ? "可逐项批准或拒绝后继续整理结果，也可停止整个任务。拒绝不会回滚已完成的操作；本轮将封锁被拒绝工具、等价远端操作及后续用户写工具，防止自动绕过；本批明确批准的动作仍可执行。" : undefined} />
      <ul className="run-approval-actions">{approval.actions.map((action, index) => <li key={`${action.name}-${index}`}>
        <strong>{action.name}</strong>
        {action.preview && Object.keys(action.preview).length > 0 && <span>操作预览：{Object.entries(action.preview).map(([key, value]) => `${key}: ${value}`).join(" · ")}</span>}
        {approval.state === "pending" && action.action_id && approval.actions.length > 1 && <div role="group" aria-label={`操作 ${index + 1} 的决定`}><Radio.Group
          value={choices[`${approval.id}:${action.action_id}`]} disabled={submitting}
          onChange={event => setChoices(current => ({ ...current, [`${approval.id}:${action.action_id}`]: event.target.value }))}>
          <Radio value="approve">批准此项</Radio><Radio value="reject">拒绝此项</Radio>
        </Radio.Group></div>}
        {action.resource_name && <span>目标文件：{action.resource_name}</span>}
        <details><summary>查看参数摘要</summary><code>{action.arguments_hash}</code></details>
      </li>)}</ul>
      <p className="run-approval-expiry">审批截止：{new Date(serverTime(approval.expires_at)).toLocaleString("zh-CN", { hour12: false })}</p>
      {approval.state === "pending" && !loadError && <Space wrap>
        {approval.actions.length > 1 && approval.actions.every(action => action.action_id) && <Button type="primary"
          disabled={submitting || approval.actions.some(action => !choices[`${approval.id}:${action.action_id}`])} onClick={() => void decide("resolve")}>提交逐项决定</Button>}
        <Button type={approval.actions.length === 1 ? "primary" : "default"} aria-label={approval.actions.length > 1 ? "同意全部执行" : "同意执行"}
          loading={submitting} disabled={submitting} onClick={() => void decide("approve")}>{approval.actions.length > 1 ? "同意全部执行" : "同意执行"}</Button>
        <Button aria-label={approval.actions.length > 1 ? "全部拒绝并继续" : "拒绝本次并继续"} disabled={submitting}
          onClick={() => void decide("reject_continue")}>{approval.actions.length > 1 ? "全部拒绝并继续" : "拒绝本次并继续"}</Button>
        <Button danger aria-label="停止整个任务" disabled={submitting} onClick={() => void decide("stop")}>停止整个任务</Button>
      </Space>}
    </>}
  </section>;
}
