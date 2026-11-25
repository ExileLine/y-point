import { useEffect, useRef, useState } from "react";
import { Alert, Button, Popconfirm, Space, Spin, Typography } from "antd";
import { getSessionApprovalPolicy, revokeSessionApprovalPolicy, type SessionApprovalPolicy as Policy } from "../api";

export default function SessionApprovalPolicy({ agentId, sessionId, revision }: { agentId: number; sessionId: string; revision: string }) {
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const inFlight = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const current = ++generation.current;
    setBusy(true);
    getSessionApprovalPolicy(agentId, sessionId, controller.signal).then(value => {
      if (current === generation.current && !controller.signal.aborted) { setPolicy(value); setError(""); }
    }).catch(failure => {
      if (current === generation.current && !controller.signal.aborted) setError(failure instanceof Error ? failure.message : "放行策略加载失败");
    }).finally(() => { if (current === generation.current && !controller.signal.aborted) setBusy(false); });
    return () => { controller.abort(); generation.current++; };
  }, [agentId, sessionId, revision, refresh]);

  async function revoke(toolId?: number) {
    if (!policy || inFlight.current) return;
    inFlight.current = true;
    const current = ++generation.current;
    setBusy(true);
    setError("");
    try {
      const updated = await revokeSessionApprovalPolicy(agentId, sessionId, policy.version, toolId);
      if (current === generation.current) setPolicy(updated);
    } catch (failure) {
      if (current === generation.current) setError(failure instanceof Error ? failure.message : "撤销失败，请刷新后重试");
    } finally {
      inFlight.current = false;
      if (current === generation.current) setBusy(false);
    }
  }

  return <details className="session-approval-policy">
    <summary>本对话 MCP 放行：{policy ? policy.tools.length ? `${policy.tools.length} 个已确认工具` : "未设置" : "加载中"}</summary>
    <Space direction="vertical" style={{ width: "100%", paddingBlock: 8 }}>
      {busy && <Spin size="small" aria-label="加载会话放行策略" />}
      {error && <Alert type="error" showIcon message={error} />}
      {policy?.force_approval && <Alert type="info" showIcon message="部署已强制审批，会话放行暂不生效" />}
      <Typography.Text type="secondary">仅覆盖确认时的工具集合和版本，不自动包含新增工具。授权失效、版本或审批配置变化后需重新确认；撤销不撤回已经执行或单次明确批准的调用。</Typography.Text>
      {policy?.tools.map(tool => <Space key={tool.tool_id} wrap>
        <span>{tool.name} · v{tool.tool_version}</span>
        <Button disabled={busy} onClick={() => void revoke(tool.tool_id)} aria-label={`撤销 ${tool.name} 的会话放行`}>撤销此工具</Button>
      </Space>)}
      <Space wrap>
        <Button disabled={busy} onClick={() => setRefresh(value => value + 1)}>刷新放行策略</Button>
        {!!policy?.tools.length && <Popconfirm title="撤销本对话全部工具放行？" description="后续调用恢复按当前审批策略确认。" onConfirm={() => revoke()} okButtonProps={{ "aria-label": "确认撤销全部放行" }} okText="撤销" cancelText="取消">
          <Button danger disabled={busy}>撤销全部放行</Button>
        </Popconfirm>}
      </Space>
    </Space>
  </details>;
}
