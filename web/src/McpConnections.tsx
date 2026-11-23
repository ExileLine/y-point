import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Empty, Input, Popconfirm, Select, Space, Switch, Tag, Typography, message } from "antd";
import { createMcpConnection, deleteMcpConnection, discoverMcpConnection, listMcpConnections, listMcpStdioProfiles, updateMcpConnection,
  type DiscoveredTool, type McpConnection, type McpConnectionDraft } from "./api";

import { McpConnectionSync } from "./McpToolSync";
import { McpConnectionShares } from "./McpConnectionAccess";

type Draft = { item: McpConnection | null; name: string; transport: "streamable_http" | "stdio"; url: string; profile: string;
  args: string; env: string; token: string; shared: boolean; enabled: boolean; clear: boolean; authType: "none" | "bearer" | "api_key"; keyHeader: string; headers: string; secrets: string; approvalMode: "writes" | "always" | "never" };
const blank = (): Draft => ({ item: null, name: "", transport: "streamable_http", url: "", profile: "", args: "[]", env: "{}", token: "", shared: false, enabled: true, clear: false, authType: "bearer", keyHeader: "X-API-Key", headers: "", secrets: "{}", approvalMode: "writes" });
const errorText = (error: unknown) => error instanceof Error ? error.message : "操作失败";

type Props = {
  open: boolean;
  onClose: () => void;
  onChanged: () => void;
  selectLabel: string;
  onSelectTool: (connection: McpConnection, tool: DiscoveredTool) => void;
};

export default function McpConnections({ open, onClose, onChanged, onSelectTool, selectLabel }: Props) {
  const [items, setItems] = useState<McpConnection[]>([]);
  const [profiles, setProfiles] = useState<{ name: string; env_keys: string[] }[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [discovery, setDiscovery] = useState<{ connection: McpConnection; tools: DiscoveredTool[] } | null>(null);
  const [discovering, setDiscovering] = useState<number | null>(null);
  async function reload() {
    setLoading(true);
    try { setItems(await listMcpConnections()); setError(""); }
    catch (failure) { setError(errorText(failure)); }
    finally { setLoading(false); }
  }
  useEffect(() => {
    if (!open) { setDraft(null); setDiscovery(null); return; }
    void reload();
    void listMcpStdioProfiles().then(setProfiles).catch(failure => setError(errorText(failure)));
    setDiscovery(null);
  }, [open]);
  const update = (change: Partial<Draft>) => setDraft(current => current ? { ...current, ...change } : current);
  async function save() {
    if (!draft) return;
    setSaving(true); setSaveError("");
    try {
      if (!draft.name.trim()) throw new Error("请填写连接名称");
      const body: McpConnectionDraft = { name: draft.name.trim(), transport: draft.transport, enabled: draft.enabled, share_credentials: draft.shared, approval_mode: draft.approvalMode };
      if (draft.transport === "streamable_http") {
        body.url = draft.url.trim();
        body.auth_type = draft.authType;
        if (draft.authType === "api_key") body.api_key_header = draft.keyHeader;
        if (draft.token && draft.authType === "bearer") body.bearer_token = draft.token;
        if (draft.token && draft.authType === "api_key") body.api_key = draft.token;
        if (draft.headers.trim()) body.headers = JSON.parse(draft.headers);
        body.sensitive_headers = JSON.parse(draft.secrets);
        for (const [label, values] of [["普通 Headers", body.headers], ["敏感 Headers", body.sensitive_headers]] as const) {
          if (values != null && (typeof values !== "object" || Array.isArray(values))) throw new Error(`${label} 须为 JSON 对象`);
        }
      } else {
        body.stdio_profile = draft.profile;
        const args: unknown = JSON.parse(draft.args), env: unknown = JSON.parse(draft.env);
        if (!Array.isArray(args) || !args.every(item => typeof item === "string")) throw new Error("启动参数必须是 JSON 字符串数组");
        if (!env || Array.isArray(env) || typeof env !== "object" || !Object.values(env).every(value => typeof value === "string")) throw new Error("环境变量必须是 JSON 字符串对象");
        body.args = args; body.env = env as Record<string, string>;
      }
      if (draft.item) await updateMcpConnection(draft.item.id, { ...body, expected_version: draft.item.version, clear_credentials: draft.clear });
      else await createMcpConnection(body);
      setDraft(null); setDiscovery(null); await reload(); onChanged(); message.success("MCP 连接已保存");
    } catch (failure) { setSaveError(failure instanceof SyntaxError ? "参数、环境变量或 Headers 不是有效 JSON" : errorText(failure)); }
    finally { setSaving(false); }
  }
  function edit(item: McpConnection) {
    setSaveError(""); setDraft({ item, name: item.name, transport: item.transport, url: item.url || "", profile: item.stdio_profile || "",
      args: JSON.stringify(item.args, null, 2), env: "{}", token: "", shared: item.share_credentials, enabled: item.enabled, clear: false, authType: item.auth_type || (item.has_credentials ? "bearer" : "none"), keyHeader: item.api_key_header || "X-API-Key", headers: "", secrets: "{}", approvalMode: item.approval_mode || "writes" });
  }
  // 管理层高于 Tool 编辑器；子编辑器继承 Drawer 层级，避免重新打开时依赖 portal 的插入顺序。
  return <Drawer rootClassName="resource-editor" title="MCP 连接" open={open} zIndex={1200} width={720} onClose={onClose} destroyOnClose>
      <Space direction="vertical" size={16} style={{ width: "100%" }}>
        <Typography.Paragraph>连接保存地址、传输方式与凭据，可供多个 Tool 复用。连接本身不出现在 Tool 目录；发现远端函数后，点击“添加为 Tool”并保存，才能关联到 Agent。</Typography.Paragraph>
        <Typography.Paragraph type="secondary">修改连接后需重新发布 Tool 才会采用新配置；停用、删除、凭据轮换和撤销共享会影响已有运行。</Typography.Paragraph>
        <Space wrap><Button type="primary" onClick={() => { setSaveError(""); setDraft(blank()); }}>新建连接</Button><Button loading={loading} onClick={() => void reload()}>刷新连接</Button></Space>
        {error && <Alert type="error" showIcon message={error} />}
        {!loading && !items.length && <Empty description="尚未配置 MCP 连接" />}
        {items.map(item => <div className="resource-surface" key={item.id}>
          <Space wrap><strong>{item.name}</strong><Tag>{item.transport === "stdio" ? "stdio" : "Streamable HTTP"}</Tag><Tag>v{item.version}</Tag><Tag>{item.enabled ? "已启用" : "已停用"}</Tag></Space>
          <p style={{ overflowWrap: "anywhere" }}>{item.url || item.stdio_profile}</p>
          <Space wrap><Button onClick={() => edit(item)}>编辑连接</Button><McpConnectionShares connection={item} />
            <McpConnectionSync connectionId={item.id} onChanged={() => { void reload(); onChanged(); }} />
            <Button disabled={!item.enabled || discovering !== null} loading={discovering === item.id} onClick={async () => {
              setDiscovering(item.id); setDiscovery(null); setError("");
              try { const found = await discoverMcpConnection(item.id); setDiscovery({ connection: item, tools: found }); }
              catch (failure) { setError(errorText(failure)); } finally { setDiscovering(null); }
            }}>测试连接并发现工具</Button>
            <Popconfirm title="删除连接会停止相关运行，确定删除？" onConfirm={async () => {
              try { await deleteMcpConnection(item.id); setDiscovery(null); await reload(); onChanged(); } catch (failure) { setError(errorText(failure)); }
            }}><Button danger>删除连接</Button></Popconfirm>
          </Space>
          {discovery?.connection.id === item.id && <Space direction="vertical" size={12} style={{ width: "100%", marginTop: 16 }}>
            <Alert type="success" showIcon message={`${item.name}：${discovery.tools.map(tool => tool.name).join("、") || "未发现工具"}`} />
            {discovery.tools.map(tool => <div className="resource-surface" key={tool.name}>
              <Typography.Text strong style={{ overflowWrap: "anywhere" }}>{tool.name}</Typography.Text>
              <Typography.Paragraph style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>{tool.description || "远端未提供说明，请在 Tool 中补充用途。"}</Typography.Paragraph>
              <Button onClick={() => onSelectTool(discovery.connection, tool)}>{selectLabel}</Button>
            </div>)}
          </Space>}
        </div>)}
      </Space>
    <Drawer rootClassName="resource-editor" title={draft?.item ? "编辑 MCP 连接" : "新建 MCP 连接"} open={draft !== null} width={560} onClose={() => { if (!saving) setDraft(null); }}
      extra={<Button type="primary" loading={saving} onClick={() => void save()}>保存连接</Button>}>
      {draft && <Space direction="vertical" size={16} style={{ width: "100%" }}>
        {saveError && <Alert type="error" showIcon message={saveError} />}
        <label>连接名称<Input aria-label="连接名称" value={draft.name} onChange={event => update({ name: event.target.value })} maxLength={64} /></label>
        <label>传输方式<Select aria-label="MCP 传输方式" style={{ width: "100%" }} value={draft.transport}
          onChange={transport => update({ transport, token: "", env: "{}", headers: "", secrets: "{}" })} options={[{ value: "streamable_http", label: "Streamable HTTP" }, { value: "stdio", label: "stdio（受控容器）" }]} /></label>
        {draft.transport === "streamable_http" ? <>
          <label>MCP 地址<Input aria-label="连接 MCP 地址" value={draft.url} placeholder="http://example.com:8080/mcp 或 https://example.com/mcp" onChange={event => update({ url: event.target.value })} /></label>
          <Typography.Text type="secondary">支持 HTTP/HTTPS、公网、内网及本机地址和自定义端口，凭据请填写在下方独立字段中。</Typography.Text>
          <label>鉴权方式<Select aria-label="MCP 鉴权方式" style={{ width: "100%" }} value={draft.authType} onChange={authType => update({ authType, token: "" })}
            options={[{ value: "none", label: "无主密钥" }, { value: "bearer", label: "Bearer" }, { value: "api_key", label: "API Key" }]} /></label>
          {draft.authType === "api_key" && <label>API Key Header<Input aria-label="API Key Header 名称" value={draft.keyHeader} onChange={event => update({ keyHeader: event.target.value })} /></label>}
          {draft.authType !== "none" && <label>{draft.authType === "bearer" ? "Bearer 凭据" : "API Key"}<Input.Password aria-label={draft.authType === "bearer" ? "连接 Bearer 凭据" : "连接 API Key"}
            autoComplete="new-password" value={draft.token} placeholder={draft.item?.has_credentials ? "留空保留当前凭据" : "填写密钥后可发现工具"} onChange={event => update({ token: event.target.value })} /></label>}
          <label>普通 Headers（JSON 对象）<Input.TextArea aria-label="普通 MCP Headers" value={draft.headers} rows={3} placeholder='留空保留；{} 清空，例如 {"X-Tenant":"team-a"}' onChange={event => update({ headers: event.target.value })} /></label>
          <label>敏感 Headers（JSON 对象）<Input.TextArea aria-label="敏感 MCP Headers" value={draft.secrets} autoComplete="off" rows={3} onChange={event => update({ secrets: event.target.value })} /></label>
          <Typography.Text type="secondary">普通已配置：{draft.item?.header_names?.join("、") || "无"}；敏感已配置：{draft.item?.sensitive_header_names?.join("、") || "无"}。只显示名称；敏感字段值为 null 时删除该项，空对象保留。密钥或租户 Headers 变化后，旧配置失效，需同步重新发布工具。</Typography.Text>
        </> : <>
          <label>启动配置<Select aria-label="stdio 启动配置" style={{ width: "100%" }} value={draft.profile || undefined} onChange={profile => update({ profile })}
            options={profiles.map(profile => ({ value: profile.name, label: profile.name }))} /></label>
          {!profiles.length && <Alert type="info" showIcon message="请管理员先配置 stdio 固定镜像与启动命令，再创建连接。" />}
          <label>追加参数（JSON 数组）<Input.TextArea aria-label="stdio 追加参数" value={draft.args} rows={3} onChange={event => update({ args: event.target.value })} /></label>
          <label>凭据环境变量（JSON 对象）<Input.TextArea aria-label="stdio 凭据环境变量" autoComplete="off" value={draft.env} rows={4} onChange={event => update({ env: event.target.value })} /></label>
          <Typography.Text type="secondary">允许的键：{profiles.find(profile => profile.name === draft.profile)?.env_keys.join("、") || "无"}。环境变量加密保存，不回显；空对象保留已有凭据。容器不访问网络或宿主机业务目录。</Typography.Text>
        </>}
        {draft.item && <label className="resource-switch-row">清除凭据并撤销历史版本<Switch aria-label="清除连接凭据" checked={draft.clear} onChange={clear => update({ clear })} /></label>}
        <label>默认审批策略<Select aria-label="连接默认审批策略" style={{ width: "100%" }} value={draft.approvalMode}
          onChange={approvalMode => update({ approvalMode })} options={[{ value: "writes", label: "写入和未知操作需审批（默认）" }, { value: "always", label: "每次调用均审批" }, { value: "never", label: "不额外审批（仍受平台约束）" }]} /></label>
        <Typography.Text type="secondary">普通非幂等写始终需要审批；工具可覆盖默认，使用者和部署可进一步收紧。收紧策略会停止原先免审批的相关运行。</Typography.Text>
        <label className="resource-switch-row">允许公开工具使用连接凭据<Switch aria-label="共享 MCP 连接凭据" checked={draft.shared} onChange={shared => update({ shared })} /></label>
        <label className="resource-switch-row">启用连接<Switch aria-label="启用 MCP 连接" checked={draft.enabled} onChange={enabled => update({ enabled })} /></label>
      </Space>}
    </Drawer>
  </Drawer>;
}
