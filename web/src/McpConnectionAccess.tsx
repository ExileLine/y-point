import { useResourceQuery } from "./hooks/useResourceQuery";
import { callabilityNotice, callabilityText, checkedTime } from "./components/ToolCallability";
import { useEffect, useState } from "react";
import { Alert, Button, Divider, Drawer, Empty, List, Popconfirm, Select, Space, Switch, Tag, Typography, message } from "antd";
import {
  listMcpConnectionCatalog, listMcpConnectionShares, revokeMcpConnectionShare, saveMcpConnectionShare, searchShareTargets,
  type AgentMcpConnection, type AgentShare, type McpConnection, type McpConnectionShare, type McpConnectionSummary,
} from "./api";

const errorText = (failure: unknown) => failure instanceof Error ? failure.message : "操作失败";

export function McpConnectionPicker({ value = [], onChange, active }: {
  value?: AgentMcpConnection[]; onChange?: (value: AgentMcpConnection[]) => void; active: boolean;
}) {
  const { data: catalog, error, loading, refresh, loadedAt } = useResourceQuery<McpConnectionSummary[]>(listMcpConnectionCatalog, [], active);
  const uncertain = loading || Boolean(error) || !loadedAt;
  function update(group: AgentMcpConnection) {
    onChange?.(value.map(item => item.connection_id === group.connection_id ? group : item));
  }
  return <Space direction="vertical" style={{ width: "100%" }}>
    <Space wrap><Button size="small" loading={loading} onClick={() => void refresh()}>刷新连接状态</Button>
      <Typography.Text type="secondary">{loading ? "正在核对连接状态…" : `目录查询时间：${checkedTime(loadedAt)}`}</Typography.Text></Space>
    <Typography.Text type="secondary">{callabilityNotice}</Typography.Text>
    {error && <Alert type="warning" showIcon message={`连接目录加载失败：${error}`} description="保留已有选择及上次成功目录，状态可能过期；可重试查询或移除关联。" action={<Button onClick={() => void refresh()}>重试目录</Button>} />}
    <Select aria-label="选择 MCP 连接" mode="multiple" style={{ width: "100%" }} loading={loading} placeholder="选择拥有或共享给我的连接"
      value={value.map(group => group.connection_id)} optionFilterProp="label"
      onChange={ids => onChange?.(ids.map((id: number) => value.find(group => group.connection_id === id) || { connection_id: id, tools: [] }))}
      options={[
        ...catalog.map(item => ({ value: item.id, label: `${item.name} · ${item.can_edit ? "我的连接" : "指定共享"}${item.enabled ? "" : " · 已停用"}`, disabled: (uncertain || !item.enabled) && !value.some(group => group.connection_id === item.id) })),
        ...value.filter(group => !catalog.some(item => item.id === group.connection_id)).map(group => ({ value: group.connection_id, label: `连接 #${group.connection_id} · ${uncertain ? "状态待核对" : "当前不可访问"}` })),
      ]} />
    {value.map(group => {
      const connection = catalog.find(item => item.id === group.connection_id);
      const choices = connection?.tools || [];
      const available = choices.filter(tool => tool.callability.status !== "blocked" && connection?.enabled);
      return <div key={group.connection_id} className="resource-surface" style={{ width: "100%", overflowWrap: "anywhere" }}>
        <Space wrap><Typography.Text strong>{connection?.name || `连接 #${group.connection_id}`}</Typography.Text><Tag>{group.tools.length} 个已选工具</Tag>
          <Button size="small" disabled={uncertain || !available.length} onClick={() => update({ ...group, tools: available.map(tool => group.tools.find(selected => selected.tool_id === tool.tool_id) || { tool_id: tool.tool_id, tool_version: tool.tool_version }) })}>选择全部可用工具</Button>
          <Button size="small" onClick={() => onChange?.(value.filter(item => item.connection_id !== group.connection_id))}>移除此连接</Button>
        </Space>
        {connection && <Typography.Paragraph type="secondary">连接配置 v{connection.version ?? "待核对"} · {connection.can_edit ? "所有者，可管理连接" : `指定共享，仅工具子集${connection.share_version != null ? ` · 授权 v${connection.share_version}` : ""}`} · {connection.allow_credentials ? "已授权凭据借用" : "未授予连接凭据借用"} · 判断时间：{checkedTime(connection.callability_checked_at)}</Typography.Paragraph>}
        {!connection && uncertain && <Typography.Paragraph type="secondary">连接状态待核对，已有选择已保留。</Typography.Paragraph>}
        {!connection && !uncertain && <Typography.Paragraph type="warning">连接授权不可用；请所有者授权或移除此连接。</Typography.Paragraph>}
        {connection && !uncertain && !choices.length && <Typography.Paragraph type="warning">当前仅可查看连接摘要，暂无可选工具；请所有者发布工具并授权子集，或移除此连接。</Typography.Paragraph>}
        <Select aria-label={`连接 ${group.connection_id} 的工具`} mode="multiple" loading={loading} style={{ width: "100%", marginTop: 12 }} optionFilterProp="label" placeholder="明确选择工具子集" value={group.tools.map(tool => tool.tool_id)}
          onChange={ids => update({ ...group, tools: ids.map((id: number) => group.tools.find(tool => tool.tool_id === id) || { tool_id: id, tool_version: choices.find(tool => tool.tool_id === id)!.tool_version }) })}
          options={[
            ...choices.map(tool => ({ value: tool.tool_id, disabled: (uncertain || tool.callability.status === "blocked") && !group.tools.some(selected => selected.tool_id === tool.tool_id),
              label: `${tool.display_name} · v${group.tools.find(selected => selected.tool_id === tool.tool_id)?.tool_version || tool.tool_version} · ${group.tools.some(selected => selected.tool_id === tool.tool_id && selected.tool_version !== tool.tool_version) ? `已固定旧版；当前发布 v${tool.tool_version}：${callabilityText(tool.callability)}；旧版提交时复核` : callabilityText(tool.callability)}${tool.mcp_connection_version != null ? ` · 当前发布固定连接配置 v${tool.mcp_connection_version}` : ""}` })),
            ...group.tools.filter(tool => !choices.some(choice => choice.tool_id === tool.tool_id)).map(tool => ({ value: tool.tool_id, label: `工具 #${tool.tool_id} · v${tool.tool_version} · ${uncertain ? "状态待核对" : "当前不可访问"}` })),
          ]} />
        {group.tools.length > 0 && <details><summary>查看已选工具状态</summary>{group.tools.map(selected => {
          const tool = choices.find(item => item.tool_id === selected.tool_id);
          return <Typography.Paragraph key={selected.tool_id}>
            <Typography.Text strong>{tool?.display_name || `工具 #${selected.tool_id}`} · 已关联 v{selected.tool_version}</Typography.Text><br />
            {tool ? <>当前发布 v{tool.tool_version}：{callabilityText(tool.callability)}<br />判断时间：{checkedTime(tool.callability_checked_at)}
              {selected.tool_version !== tool.tool_version && <><br />已固定旧版，以上是当前发布版本的预检，旧版在提交时另行复核。</>}</>
              : uncertain ? "状态待核对，原固定版本已保留。" : "当前不可访问，请所有者授权或移除此工具。"}
          </Typography.Paragraph>;
        })}</details>}
      </div>;
    })}
  </Space>;
}

export function McpConnectionShares({ connection }: { connection: McpConnection }) {
  const [open, setOpen] = useState(false);
  const [shares, setShares] = useState<McpConnectionShare[]>([]);
  const [catalog, setCatalog] = useState<McpConnectionSummary | null>(null);
  const [targets, setTargets] = useState<AgentShare[]>([]);
  const [draft, setDraft] = useState<{ userId: number | undefined; version: number; tools: number[]; credentials: boolean }>({ userId: undefined, version: 0, tools: [], credentials: false });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!open) return;
    let live = true;
    setLoading(true); setError("");
    Promise.all([listMcpConnectionShares(connection.id), listMcpConnectionCatalog()]).then(([rows, groups]) => {
      if (live) { setShares(rows); setCatalog(groups.find(group => group.id === connection.id) || null); }
    }).catch(failure => { if (live) { setCatalog(null); setError(errorText(failure)); } }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [open, connection.id, revision]);
  useEffect(() => {
    let live = true;
    setTargets([]);
    if (!open || query.trim().length < 2) return;
    const timer = window.setTimeout(() => {
      searchShareTargets(query.trim()).then(rows => { if (live) setTargets(rows); }).catch(failure => { if (live) setError(errorText(failure)); });
    }, 250);
    return () => { live = false; window.clearTimeout(timer); };
  }, [open, query]);
  function reset() { setDraft({ userId: undefined, version: 0, tools: [], credentials: false }); setQuery(""); }
  async function save() {
    if (draft.userId === undefined) { setError("请先选择指定用户"); return; }
    setSaving(true); setError("");
    try {
      await saveMcpConnectionShare(connection.id, draft.userId, { expected_version: draft.version, tool_ids: draft.tools, allow_credentials: draft.credentials });
      message.success("连接授权已保存"); reset(); setRevision(x => x + 1);
    } catch (failure) { setError(errorText(failure)); }
    finally { setSaving(false); }
  }
  return <>
    <Button onClick={() => { reset(); setOpen(true); }}>连接共享</Button>
    <Drawer title={`${connection.name} · 指定用户共享`} width={600} open={open} onClose={() => { if (!saving) setOpen(false); }} destroyOnClose>
      <Space direction="vertical" size={16} style={{ width: "100%", overflowWrap: "anywhere" }}>
        <Alert type="info" showIcon message="分别授权连接摘要、工具子集与凭据借用" description="空工具集只共享摘要；新发布工具不会自动加入。接收者不能查看连接地址、密钥、Headers 或容器配置，也不能编辑连接。共享 Agent 需另行授权。" />
        {error && <Alert type="error" showIcon message={error} action={<Button disabled={saving} onClick={() => setRevision(x => x + 1)}>刷新授权</Button>} />}
        <Select aria-label="连接共享用户" showSearch filterOption={false} onSearch={setQuery} placeholder="输入至少两个字符搜索用户名" style={{ width: "100%" }} disabled={saving || draft.version > 0} value={draft.userId}
          onChange={userId => { const prior = shares.find(share => share.user_id === userId); setDraft({ userId, version: prior?.version || 0, tools: prior?.tool_ids || [], credentials: prior?.allow_credentials || false }); }}
          options={[...targets, ...shares.filter(share => !targets.some(target => target.user_id === share.user_id))].map(user => ({ value: user.user_id, label: user.username }))} />
        <Typography.Text>允许执行的工具子集</Typography.Text>
        <Select aria-label="共享连接的工具子集" mode="multiple" style={{ width: "100%" }} loading={loading} disabled={loading || saving || !catalog} optionFilterProp="label" value={draft.tools} onChange={tools => setDraft({ ...draft, tools })}
          options={[...(catalog?.tools || []).map(tool => ({ value: tool.tool_id, label: `${tool.display_name} · v${tool.tool_version}` })), ...draft.tools.filter(id => !catalog?.tools.some(tool => tool.tool_id === id)).map(id => ({ value: id, label: `工具 #${id} · 已移除或不可用` }))]} />
        <Space wrap><Switch aria-label="允许指定用户借用凭据" checked={draft.credentials} disabled={saving} onChange={credentials => setDraft({ ...draft, credentials })} /><Typography.Text>允许指定用户借用授权工具的凭据</Typography.Text></Space>
        <Typography.Text type="secondary">关闭此项仍可执行无凭据工具；公开工具已有的公开凭据借用是独立授权。收紧或撤销将取消该用户相关的活动运行。</Typography.Text>
        <Space wrap><Button type="primary" loading={saving} disabled={loading || !catalog} onClick={() => void save()}>保存连接授权</Button><Button disabled={saving} onClick={reset}>清空选择</Button></Space>
        <Divider>已有指定用户授权</Divider>
        <List loading={loading} dataSource={shares} locale={{ emptyText: <Empty description="尚无指定用户授权" /> }} renderItem={share => <List.Item>
          <Space direction="vertical" style={{ width: "100%" }}><Typography.Text strong>{share.username}</Typography.Text>
            <Typography.Text>{share.tool_ids.length} 个工具 · {share.allow_credentials ? "可借用凭据" : "未授予凭据借用"}</Typography.Text>
            <Space wrap><Button disabled={saving} onClick={() => setDraft({ userId: share.user_id, version: share.version, tools: share.tool_ids, credentials: share.allow_credentials })}>修改授权</Button>
              <Popconfirm title="撤销连接授权并取消该用户相关运行？" onConfirm={async () => {
                setSaving(true); setError("");
                try { await revokeMcpConnectionShare(connection.id, share); reset(); setRevision(x => x + 1); message.success("连接授权已撤销"); }
                catch (failure) { setError(errorText(failure)); }
                finally { setSaving(false); }
              }}><Button danger disabled={saving}>撤销连接授权</Button></Popconfirm>
            </Space>
          </Space>
        </List.Item>} />
      </Space>
    </Drawer>
  </>;
}
