import { useState } from "react";
import { Alert, Button, Checkbox, Drawer, Empty, Input, Space, Tag, Typography } from "antd";
import { applyAgentToolUpgrades, applyMcpSync, previewAgentToolUpgrades, previewMcpSync,
  type AgentToolUpgradePreview, type AgentToolUpgradeRequest, type McpBatchResult, type McpSyncPreview, type McpSyncRequest } from "./api";

const text = (error: unknown) => error instanceof Error ? error.message : "操作失败";
const changeNames: Record<string, string> = { schema: "参数结构变化", input_schema: "参数结构变化", annotations: "远端声明变化", mcp_annotations: "远端声明变化",
  execution_config: "连接或凭据变化", revoked_config: "历史配置已撤销", mcp_config: "执行配置变化", credential_sharing: "凭据共享变化", share_credentials: "凭据共享变化",
  approval_mode: "审批策略变化", effect: "动作性质变化", disabled: "工具已停用", access: "授权变化" };
const stateNames = { added: "新增", changed: "有变化", removed: "远端已移除", unchanged: "无变化" };
function SchemaDiff({ previous, current }: { previous: unknown; current: unknown }) {
  return <details><summary>查看参数结构差异</summary><Typography.Paragraph>原版本</Typography.Paragraph>
    <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(previous, null, 2)}</pre>
    <Typography.Paragraph>目标版本</Typography.Paragraph><pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(current, null, 2)}</pre></details>;
}
function Results({ result }: { result: McpBatchResult | null }) {
  if (!result) return null;
  return <Space direction="vertical" style={{ width: "100%" }}>{result.items.map((row, index) => <Alert key={index} showIcon
    type={row.status === "failed" ? "error" : "success"} message={`${row.remote_name || `Tool #${row.tool_id}`} · ${row.status === "failed" ? "未完成" : row.status === "disabled" ? "已停用" : "已完成"}`}
    description={row.message || (row.version ? `版本 v${row.version}` : undefined)} />)}</Space>;
}
export function McpConnectionSync({ connectionId, onChanged }: { connectionId: number; onChanged: () => void }) {
  const [open, setOpen] = useState(false), [plan, setPlan] = useState<McpSyncPreview | null>(null);
  const [selected, setSelected] = useState<string[]>([]), [reasons, setReasons] = useState<Record<string, string>>({});
  const [writes, setWrites] = useState<Record<string, boolean>>({}), [loading, setLoading] = useState(false), [saving, setSaving] = useState(false);
  const [error, setError] = useState(""), [result, setResult] = useState<McpBatchResult | null>(null), [pending, setPending] = useState<McpSyncRequest | null>(null);
  const key = (row: { tool_id: number | null; remote_name: string }) => `${row.tool_id || "new"}:${row.remote_name}`;
  async function preview() {
    setOpen(true); setLoading(true); setError("");
    try { setPlan(await previewMcpSync(connectionId)); setSelected([]); setResult(null); setPending(null); setReasons({}); setWrites({}); }
    catch (failure) { setPlan(null); setError(text(failure)); }
    finally { setLoading(false); }
  }
  async function confirm() {
    if (!plan) return;
    const body = pending || { request_id: crypto.randomUUID(), expected_version: plan.connection_version, preview_hash: plan.preview_hash,
      selections: plan.items.filter(row => selected.includes(key(row))).map(row => ({ tool_id: row.tool_id, remote_name: row.remote_name,
        expected_version: row.current_version, action: row.state === "removed" ? "disable" as const : "publish" as const,
        ...(reasons[key(row)] ? { read_only_reason: reasons[key(row)] } : {}), ...(writes[key(row)] ? { effect: "write" as const, recovery_policy: "manual" as const } : {}) })) };
    setPending(body); setSaving(true); setError("");
    try { setResult(await applyMcpSync(connectionId, body)); setPending(null); onChanged(); }
    catch (failure) { setError(`${text(failure)}。可重试本次提交；状态已变化时请重新预览。`); }
    finally { setSaving(false); }
  }
  return <><Button onClick={() => void preview()}>同步差异</Button><Drawer rootClassName="resource-editor" title="MCP 工具同步预览" open={open} width={680} zIndex={1300}
    onClose={() => { if (!saving) setOpen(false); }}><Space direction="vertical" size={16} style={{ width: "100%" }}>
      <Typography.Paragraph>选择后才发布新版本；新工具默认私有，需再关联 Agent。已有 Agent 和历史运行保留原工具版本。远端移除项确认后停用并停止相关运行。</Typography.Paragraph>
      <Button loading={loading} disabled={saving} onClick={() => void preview()}>重新预览</Button>{error && <Alert type="error" showIcon message={error} />}
      {plan && !loading && !result && plan.items.map(row => <div className="resource-surface" key={key(row)}>
        <Checkbox disabled={saving || Boolean(pending) || row.state === "unchanged"} checked={selected.includes(key(row))}
          onChange={event => setSelected(current => event.target.checked ? [...current, key(row)] : current.filter(value => value !== key(row)))}>
          {row.display_name} · {stateNames[row.state]}{row.current_version ? ` · v${row.current_version}` : ""}</Checkbox>
        <p>远端函数：{row.remote_name}</p><Space wrap>{row.changes.map(value => <Tag key={value}>{changeNames[value] || value}</Tag>)}</Space>
        {row.requires_read_confirmation && <><Alert type="warning" showIcon message="执行定义变化，保留只读需重新确认" />
          <Input aria-label={`${row.display_name} 只读确认理由`} disabled={saving || Boolean(pending)} value={reasons[key(row)] || ""}
            placeholder="填写已核对只读性质的理由" onChange={event => setReasons(current => ({ ...current, [key(row)]: event.target.value }))} /></>}
        {(row.requires_read_confirmation || row.requires_idempotency_verification) && <Checkbox disabled={saving || Boolean(pending)} checked={writes[key(row)] || false}
          onChange={event => setWrites(current => ({ ...current, [key(row)]: event.target.checked }))}>改按普通写发布，调用前审批</Checkbox>}
        {row.requires_idempotency_verification && <Typography.Paragraph type="warning">新版本需重新验收幂等恢复契约；也可改按普通写发布。</Typography.Paragraph>}
        <SchemaDiff previous={row.previous_schema} current={row.schema} />
      </div>)}
      {plan && !loading && !plan.items.length && <Empty description="没有远端或已绑定工具" />}
      {!result && <Button type="primary" loading={saving} disabled={loading || (!pending && !selected.length)} onClick={() => void confirm()}>{pending ? "重试本次提交" : "确认发布或停用所选工具"}</Button>}
      <Results result={result} />{result && <Typography.Paragraph>未完成项修复后请重新预览；成功项已保存。Agent 版本升级需在 Agent 详情中单独确认。</Typography.Paragraph>}
    </Space></Drawer></>;
}
export function AgentToolUpgrades({ agentId, onChanged }: { agentId: number; onChanged: () => void }) {
  const [plan, setPlan] = useState<AgentToolUpgradePreview | null>(null), [selected, setSelected] = useState<number[]>([]);
  const [error, setError] = useState(""), [loading, setLoading] = useState(false), [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<AgentToolUpgradeRequest | null>(null), [result, setResult] = useState<McpBatchResult | null>(null);
  async function preview() {
    setLoading(true); setError("");
    try { setPlan(await previewAgentToolUpgrades(agentId)); setSelected([]); setResult(null); setPending(null); }
    catch (failure) { setPlan(null); setError(text(failure)); }
    finally { setLoading(false); }
  }
  async function confirm() {
    if (!plan) return;
    const body = pending || { request_id: crypto.randomUUID(), expected_version: plan.agent_version, preview_hash: plan.preview_hash,
      selections: plan.items.filter(row => selected.includes(row.tool_id)).map(row => ({ tool_id: row.tool_id, expected_version: row.current_version, target_version: row.target_version })) };
    setPending(body); setSaving(true); setError("");
    try { setResult(await applyAgentToolUpgrades(agentId, body)); setPending(null); onChanged(); }
    catch (failure) { setError(`${text(failure)}。可重试本次提交，或重新预览。`); }
    finally { setSaving(false); }
  }
  return <Space direction="vertical" size={12} style={{ width: "100%", marginTop: 20 }}><Button loading={loading} disabled={saving} onClick={() => void preview()}>检查工具版本更新</Button>
    {error && <Alert type="error" showIcon message={error} />}{plan && !result && plan.items.filter(row => row.changed).map(row => <div className="resource-surface" key={row.tool_id}>
      <Checkbox disabled={saving || Boolean(pending) || row.callability.status === "blocked"} checked={selected.includes(row.tool_id)} onChange={event =>
        setSelected(current => event.target.checked ? [...current, row.tool_id] : current.filter(id => id !== row.tool_id))}>{row.display_name} · v{row.current_version} → v{row.target_version}</Checkbox>
      <Typography.Paragraph>{row.callability.message}</Typography.Paragraph><Space wrap>{row.changes.map(value => <Tag key={value}>{changeNames[value] || value}</Tag>)}</Space>
      <SchemaDiff previous={row.previous_schema} current={row.schema} /></div>)}
    {plan && !plan.items.some(row => row.changed) && <Typography.Paragraph>已关联工具暂无版本更新。</Typography.Paragraph>}
    {plan && !result && <Button type="primary" disabled={!pending && !selected.length} loading={saving} onClick={() => void confirm()}>{pending ? "重试工具升级" : "确认升级所选工具"}</Button>}
    <Results result={result} />
  </Space>;
}
