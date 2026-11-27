import { useEffect, useRef, useState } from "react";
import { Alert, Button, Modal, Progress, Tag, Typography } from "antd";
import { ApiError, testModelCapability, type Model } from "../api";
import { testEmbeddingModel } from "../knowledge-api";

type CapabilityOption = { value: string; label: string };
type VerificationItem = CapabilityOption & {
  state: "pending" | "running" | "success" | "failure" | "skipped";
  detail?: string;
};

const stateLabels = { pending: "待验证", running: "验证中", success: "通过", failure: "未通过", skipped: "未执行" };
const stateColors = { pending: "default", running: "processing", success: "success", failure: "error", skipped: "default" };

export function ModelCapabilityVerification({ model, capabilities, failureLabels, onUpdated, onClose }: {
  model: Model; capabilities: CapabilityOption[]; failureLabels: Record<string, string>;
  onUpdated: () => Promise<void>; onClose: () => void;
}) {
  const [items, setItems] = useState<VerificationItem[]>(() => capabilities
    .filter(item => model.capabilities.includes(item.value)).map(item => ({ ...item, state: "pending" })));
  const [phase, setPhase] = useState<"ready" | "running" | "finished">("ready");
  const [stopping, setStopping] = useState(false);
  const [stopped, setStopped] = useState(false);
  const active = useRef(false);
  const stopRequested = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    stopRequested.current = false;
    return () => { mounted.current = false; stopRequested.current = true; };
  }, []);

  const completed = items.filter(item => item.state === "success" || item.state === "failure").length;
  const passed = items.filter(item => item.state === "success").length;
  const failed = items.filter(item => item.state === "failure").length;

  function updateItem(value: string, changes: Partial<VerificationItem>) {
    if (mounted.current) setItems(current => current.map(item => item.value === value ? { ...item, ...changes } : item));
  }

  async function start() {
    if (active.current || phase !== "ready" || !items.length) return;
    active.current = true;
    setPhase("running");
    try {
      for (const item of items) {
        if (stopRequested.current) break;
        updateItem(item.value, { state: "running" });
        try {
          const result = item.value === "embedding"
            ? await testEmbeddingModel(model.id).then(response => ({
              available: response.success, latency_ms: response.latency_ms, failure: response.failure_code,
              detail: response.success && response.dimensions ? `${response.dimensions} 维` : undefined,
            }))
            : (await testModelCapability(model.id, item.value)).result;
          const detail = result.available
            ? `${result.latency_ms} ms${"detail" in result && result.detail ? ` · ${result.detail}` : ""}`
            : `${failureLabels[result.failure || ""] || "能力验证未通过"} · ${result.latency_ms} ms`;
          updateItem(item.value, { state: result.available ? "success" : "failure", detail });
        } catch (error) {
          updateItem(item.value, { state: "failure", detail: error instanceof Error ? error.message : "验证请求失败" });
          if (error instanceof ApiError && [401, 403, 404, 409].includes(error.status)) stopRequested.current = true;
        }
      }
    } finally {
      active.current = false;
      if (mounted.current) {
        setItems(current => current.map(item => item.state === "pending" ? { ...item, state: "skipped" } : item));
        setStopped(stopRequested.current);
        setPhase("finished");
        await onUpdated();
      }
    }
  }

  return <Modal title={`一键验证能力 · ${model.display_name}`} open onCancel={onClose}
    closable={phase !== "running"} maskClosable={phase !== "running"} keyboard={phase !== "running"}
    footer={phase === "ready" ? <>
      <Button onClick={onClose}>取消</Button>
      <Button type="primary" disabled={!items.length} onClick={() => void start()}>开始验证（{items.length} 项）</Button>
    </> : phase === "running" ? <Button disabled={stopping} onClick={() => {
      stopRequested.current = true;
      setStopping(true);
    }}>{stopping ? "正在等待当前项结束…" : "停止后续验证"}</Button> : <Button type="primary" onClick={onClose}>完成</Button>}>
    <div className="catalog-verification">
      {phase === "ready" && <Alert type="warning" showIcon message="验证可能产生上游费用"
        description="将依次验证此模型已声明的全部能力，包括已验证项。单项失败会继续下一项；可停止后续验证，已发起的调用仍会完成。" />}
      {phase === "running" && <Typography.Paragraph role="status" aria-live="polite">
        {stopping ? "已请求停止，当前调用结束后不再发起新验证。" : "正在依次验证，请保留当前页面。"}
      </Typography.Paragraph>}
      {phase === "finished" && <Alert type={failed ? "warning" : stopped ? "info" : "success"} showIcon
        message={`${stopped ? "验证已停止" : "验证完成"}：通过 ${passed} 项，未通过 ${failed} 项，未执行 ${items.length - completed} 项`} />}
      {phase !== "ready" && <div aria-label={`验证进度 ${completed}/${items.length}`}>
        <Progress percent={items.length ? Math.round(completed / items.length * 100) : 0} status={phase === "running" ? "active" : "normal"}
          format={() => `${completed}/${items.length}`} />
      </div>}
      {!items.length && <Alert type="info" showIcon message="此模型尚未声明能力，请先编辑模型，或使用单项验证。" />}
      <ul className="catalog-verification-items" aria-label="逐项验证结果" aria-live="polite">
        {items.map(item => <li key={item.value}>
          <div><Typography.Text strong>{item.label}</Typography.Text>
            <Tag color={stateColors[item.state]}>{stateLabels[item.state]}</Tag></div>
          {item.detail && <Typography.Text type="secondary">{item.detail}</Typography.Text>}
        </li>)}
      </ul>
    </div>
  </Modal>;
}
