import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Empty, Input, Modal, Popconfirm, Segmented, Select, Space, Switch, Tabs, Tag, Typography, message } from "antd";
import { Code2, Plug, Wrench, FlaskConical, Pencil, Plus, RefreshCw, Search, Trash2 } from "lucide-react";
import {
  createUserTool, deleteUserTool, discoverMcpTools, getUserTool, listToolCatalog, testUserTool, updateUserTool, retryToolEnvironment,
  type DiscoveredTool, type ToolCatalog, type ToolDraft, type UserTool,
} from "./api";

import { PageHeader, ResourceCard, ResourceCollection } from "./components/ResourcePage";

const emptyCatalog: ToolCatalog = { builtin: [], user_tools: [] };
const defaultSchema = JSON.stringify({ type: "object", properties: {}, additionalProperties: false }, null, 2);
const defaultSource = "import json\nimport sys\n\nargs = json.load(sys.stdin)\nprint(json.dumps({\"result\": args}, ensure_ascii=False))\n";

type Editor = {
  item: UserTool | null;
  kind: "script" | "mcp";
  name: string;
  description: string;
  visibility: "private" | "public";
  enabled: boolean;
  schema: string;
  language: "python" | "bash";
  source: string;
  requirements: string;
  url: string;
  remoteName: string;
  bearerToken: string;
  shareCredentials: boolean;
};

function blankEditor(): Editor {
  return {
    item: null, kind: "script", name: "", description: "", visibility: "private", enabled: true,
    schema: defaultSchema, language: "python", source: defaultSource, requirements: "",
    url: "", remoteName: "", bearerToken: "", shareCredentials: false,
  };
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

export default function Tools({ userId }: { userId: number }) {
  const [catalog, setCatalog] = useState<ToolCatalog>(emptyCatalog);
  const [query, setQuery] = useState("");
  const [kindFilter, setKindFilter] = useState("all");
  const [scope, setScope] = useState("all");
  const [editorError, setEditorError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [view, setView] = useState("user");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [saving, setSaving] = useState(false);
  const [discovered, setDiscovered] = useState<DiscoveredTool[]>([]);
  const [discovering, setDiscovering] = useState(false);
  const [testing, setTesting] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [testArgs, setTestArgs] = useState("{}");
  const [testResult, setTestResult] = useState<string | null>(null);

  async function reload() {
    setLoading(true);
    try { setCatalog(await listToolCatalog()); setError(""); }
    catch (failure) { setError(errorText(failure)); }
    finally { setLoading(false); }
  }

  useEffect(() => { void reload(); }, []);

  const environmentState = editor?.item?.environment?.state;
  const environmentPending = environmentState === "pending" || environmentState === "building";
  useEffect(() => {
    const id = editor?.item?.id;
    const version = editor?.item?.current_version;
    if (!id || !environmentPending) return;
    let active = true;
    const timer = window.setInterval(() => {
      void getUserTool(id).then((full) => {
        if (!active || full.current_version !== version) return;
        setEditor((current) => current?.item?.id === id && current.item.current_version === version ? { ...current, item: full } : current);
      }).catch(() => { /* 单次轮询失败交由下一次重试，保留用户未保存内容。 */ });
    }, 2000);
    return () => { active = false; window.clearInterval(timer); };
  }, [editor?.item?.id, editor?.item?.current_version, environmentPending]);

  function requirementLines(value: string): string[] {
    return value.split(/\r?\n/).map((line) => line.replace(/\s+#.*$/, "").trim()).filter((line) => line && !line.startsWith("#"));
  }

  async function importRequirements(file: File) {
    if (!/\.txt$/i.test(file.name) || file.size > 16 * 1024) {
      message.error("请导入 16 KiB 以内的 requirements.txt 文件");
      return;
    }
    try { update({ requirements: requirementLines(await file.text()).join("\n") }); }
    catch { message.error("依赖清单读取失败"); }
  }

  async function retryEnvironment() {
    if (!editor?.item) return;
    if (JSON.stringify(requirementLines(editor.requirements).sort()) !== JSON.stringify([...(editor.item.requirements || [])].sort())) {
      message.warning("依赖声明已修改，请先保存后再准备环境");
      return;
    }
    setRetrying(true);
    try {
      const full = await retryToolEnvironment(editor.item.id, editor.item.current_version);
      update({ item: full, enabled: full.enabled, visibility: full.visibility });
      setTestResult(null);
      await reload();
      message.success("已重新排队准备依赖环境，准备成功后请重新试运行");
    } catch (failure) { message.error(errorText(failure)); }
    finally { setRetrying(false); }
  }

  async function openEditor(item?: UserTool) {
    setEditorError("");
    setDiscovered([]);
    setTestResult(null);
    if (!item) { setEditor(blankEditor()); return; }
    try {
      const full = await getUserTool(item.id);
      setEditor({
        item: full, kind: full.kind, name: full.name, description: full.description,
        visibility: full.visibility, enabled: full.enabled,
        schema: JSON.stringify(full.input_schema || {}, null, 2), language: full.script_language || "python",
        requirements: (full.requirements || []).join("\n"),
        source: full.source_code || "", url: full.mcp_url || "", remoteName: full.mcp_tool_name || "",
        bearerToken: "", shareCredentials: Boolean(full.share_credentials),
      });
    } catch (failure) { message.error(errorText(failure)); }
  }

  function update(change: Partial<Editor>) {
    setEditor((current) => current ? { ...current, ...change } : current);
  }

  async function discover() {
    if (!editor) return;
    setDiscovering(true);
    try {
      const found = await discoverMcpTools(editor.url.trim(), editor.bearerToken || undefined);
      setDiscovered(found);
      if (!found.some((item) => item.name === editor.remoteName)) update({ remoteName: found[0]?.name || "" });
    } catch (failure) { message.error(errorText(failure)); }
    finally { setDiscovering(false); }
  }

  async function importScript(file: File) {
    if (!/\.(py|sh)$/i.test(file.name) || file.size > 64 * 1024) {
      message.error("仅支持 64 KiB 以内的 .py 或 .sh 文件");
      return;
    }
    const language = file.name.toLowerCase().endsWith(".py") ? "python" : "bash";
    if (editor?.item && editor.item.script_language !== language) {
      message.error("已有 Tool 不能更改脚本语言，请导入同语言脚本或新建 Tool");
      return;
    }
    try { update({ language, source: await file.text() }); }
    catch { message.error("脚本文件读取失败"); }
  }

  async function save() {
    if (!editor) return;
    if (!editor.name.trim() || !editor.description.trim()) { setEditorError("请填写名称和用途"); return; }
    setSaving(true); setEditorError("");
    try {
      const draft: ToolDraft = {
        name: editor.name.trim(), description: editor.description.trim(), kind: editor.kind,
        visibility: editor.kind === "script" && !editor.item ? "private" : editor.visibility,
      };
      if (editor.kind === "script") {
        draft.input_schema = JSON.parse(editor.schema) as Record<string, unknown>;
        draft.script_language = editor.language;
        draft.source_code = editor.source;
        draft.requirements = editor.language === "python" ? requirementLines(editor.requirements) : [];
      } else {
        draft.mcp_url = editor.url.trim();
        draft.mcp_tool_name = editor.remoteName;
        if (editor.bearerToken) draft.mcp_bearer_token = editor.bearerToken;
        draft.share_credentials = editor.shareCredentials;
      }
      let saved: UserTool;
      if (editor.item) {
        const { name: _name, kind: _kind, script_language: _language, ...changes } = draft;
        void _name; void _kind; void _language;
        saved = await updateUserTool(editor.item.id, { ...changes, enabled: editor.enabled, expected_version: editor.item.current_version });
        message.success(editor.kind === "script" && !saved.verified ? "脚本已保存为私有待试运行；试运行通过后可公开" : "Tool 已保存");
      } else {
        saved = await createUserTool(draft);
        message.success("Tool 已保存");
      }
      setEditor(saved.kind === "script" ? {
        ...editor, item: saved, visibility: saved.visibility, enabled: saved.enabled,
        requirements: (saved.requirements || []).join("\n"),
      } : null);
      setTestResult(null);
      await reload();
    } catch (failure) { setEditorError(failure instanceof SyntaxError ? "参数结构不是有效 JSON" : errorText(failure)); }
    finally { setSaving(false); }
  }

  async function test() {
    if (!editor?.item) return;
    if (editor.source !== (editor.item.source_code || "") || editor.schema !== JSON.stringify(editor.item.input_schema || {}, null, 2)
      || JSON.stringify(requirementLines(editor.requirements).sort()) !== JSON.stringify([...(editor.item.requirements || [])].sort())) {
      message.warning("请先保存脚本修改，再试运行当前版本");
      return;
    }
    if (editor.item.environment && !["ready", "not_required"].includes(editor.item.environment.state)) {
      message.warning("请等待依赖环境准备成功后再试运行");
      return;
    }
    setTesting(true);
    try {
      const args = JSON.parse(testArgs) as Record<string, unknown>;
      const result = await testUserTool(editor.item.id, args);
      setTestResult(result.result);
      const full = await getUserTool(editor.item.id);
      update({ item: full, enabled: full.enabled, visibility: full.visibility });
      await reload();
    } catch (failure) { message.error(failure instanceof SyntaxError ? "试运行参数不是有效 JSON" : errorText(failure)); }
    finally { setTesting(false); }
  }

  const filtered = catalog.user_tools.filter((item) => `${item.name} ${item.description}`.toLowerCase().includes(query.trim().toLowerCase())
    && (kindFilter === "all" || item.kind === kindFilter) && (scope === "all" || (scope === "mine" ? item.owner_id === userId : item.owner_id !== userId)));
  const builtins = catalog.builtin.filter((item) => `${item.model_name} ${item.description}`.toLowerCase().includes(query.trim().toLowerCase()));
  const toolIcon = (item: UserTool) => item.kind === "mcp" ? <Plug size={20} /> : <Code2 size={20} />;
  const tags = (item: UserTool) => <><Tag>{item.visibility === "public" ? "公开" : "私有"}</Tag>
    <Tag color={item.enabled ? "success" : item.kind === "script" && !item.verified ? "warning" : "default"}>{item.enabled ? "已启用" : item.kind === "script" && !item.verified ? "待试运行" : "已停用"}</Tag></>;
  const actions = (item: UserTool) => item.owner_id === userId ? <Space wrap size="small">
    <Button type="text" icon={<Pencil size={16} />} aria-label="编辑" onClick={() => void openEditor(item)}>编辑</Button>
    <Popconfirm title="删除 Tool？" description="删除后将无法继续调用此工具。" onConfirm={async () => { try { await deleteUserTool(item.id); await reload(); } catch (failure) { message.error(errorText(failure)); } }}>
      <Button type="text" danger icon={<Trash2 size={16} />} aria-label="删除">删除</Button>
    </Popconfirm>
  </Space> : <span className="resource-muted">他人公开 · 只读</span>;
  return <div className="tools-page resource-page">
    <PageHeader title="Tool" description="连接脚本与 MCP 服务，让 Agent 安全地执行操作。" actions={<Button type="primary" icon={<Plus size={16} />} onClick={() => void openEditor()}>新建 Tool</Button>} />
    <section className="resource-surface" aria-label="Tool 列表">
      <Tabs activeKey={view} onChange={setView} items={[{ key: "user", label: `用户 Tool (${catalog.user_tools.length})` }, { key: "builtin", label: `内置 Tool (${catalog.builtin.length})` }]} />
      <div className="resource-toolbar"><div className="resource-filters">
        <Input aria-label="搜索 Tool" placeholder="搜索名称或用途" prefix={<Search size={16} />} allowClear value={query} onChange={(event) => setQuery(event.target.value)} />
        {view === "user" && <><Select aria-label="Tool 类型" value={kindFilter} onChange={setKindFilter} options={[{ value: "all", label: "全部类型" }, { value: "script", label: "脚本" }, { value: "mcp", label: "MCP" }]} />
          <Select aria-label="Tool 归属" value={scope} onChange={setScope} options={[{ value: "all", label: "全部可见" }, { value: "mine", label: "我的 Tool" }, { value: "public", label: "他人公开" }]} /></>}
      </div><Space><span className="resource-count">{view === "user" ? filtered.length : builtins.length} 个结果</span><Button icon={<RefreshCw size={16} />} loading={loading} aria-label="刷新" onClick={() => void reload()} /></Space></div>
      {error && <Alert className="resource-alert" type="error" showIcon message={error} action={<Button size="small" onClick={() => void reload()}>重试</Button>} />}
      {(!error || catalog.user_tools.length + catalog.builtin.length > 0) && (view === "user" ? <ResourceCollection rowKey="id" items={filtered} loading={loading}
        empty={<Empty description={catalog.user_tools.length ? "没有匹配的 Tool，请调整筛选条件" : "创建脚本或连接 MCP 服务，扩展 Agent 的能力"} />}
        columns={[
          { title: "名称", dataIndex: "name", render: (name: string, item: UserTool) => <span className="resource-title-cell">{toolIcon(item)}<strong>{name}</strong></span> },
          { title: "用途", dataIndex: "description", ellipsis: true },
          { title: "类型", dataIndex: "kind", render: (value: string) => value === "mcp" ? "MCP" : "脚本" },
          { title: "状态", render: (_: unknown, item: UserTool) => <div className="resource-tags">{tags(item)}</div> },
          { title: "版本", dataIndex: "current_version", render: (value: number) => `v${value}` },
          { title: "操作", render: (_: unknown, item: UserTool) => actions(item) },
        ]} renderCard={(item) => <ResourceCard icon={toolIcon(item)} title={item.name} description={item.description} tags={tags(item)} meta={`${item.kind === "mcp" ? "MCP" : item.script_language === "bash" ? "Bash 脚本" : "Python 脚本"} · v${item.current_version}`} actions={actions(item)} />} />
        : <ResourceCollection rowKey="model_name" items={builtins} loading={loading} empty={<Empty description="没有匹配的内置工具" />}
          columns={[{ title: "名称", dataIndex: "model_name" }, { title: "用途", dataIndex: "description" }]}
          renderCard={(item) => <ResourceCard icon={<Wrench size={20} />} title={item.model_name} description={item.description} tags={<Tag>系统内置</Tag>} />} />)}
      <p className="resource-note">脚本保存后需通过试运行才能启用；Python 依赖环境的准备进度可在编辑面板查看。</p>
    </section>
    <Drawer rootClassName="resource-editor" title={editor?.item ? "编辑 Tool" : "新建 Tool"} open={Boolean(editor)} onClose={() => { if (!saving && !testing && !discovering && !retrying) setEditor(null); }}
      width="min(100vw, var(--yp-editor-width))" extra={<Button type="primary" loading={saving} onClick={() => void save()}>保存</Button>}>
      {editor && <Space direction="vertical" size="middle" className="resource-full-width">
        {editorError && <Alert type="error" showIcon message={editorError} description="输入内容已保留，请检查后重新保存。" />}
        <Segmented aria-label="工具类型" block value={editor.kind} onChange={(kind) => update({ kind: kind as Editor["kind"] })} disabled={Boolean(editor.item)}
          options={[{ label: "脚本", value: "script" }, { label: "MCP", value: "mcp" }]} />
        <label htmlFor="tool-name">名称<Input id="tool-name" value={editor.name} disabled={Boolean(editor.item)} maxLength={64} onChange={(event) => update({ name: event.target.value })} /></label>
        <label htmlFor="tool-description">用途<Input.TextArea id="tool-description" aria-label="用途" value={editor.description} maxLength={1024} rows={3} onChange={(event) => update({ description: event.target.value })} /></label>
        <label>可见性<Select aria-label="可见性" style={{ width: "100%" }} value={editor.visibility} disabled={editor.kind === "script" && !editor.item} onChange={(visibility) => update({ visibility })}
          options={[{ value: "private", label: "私有" }, { value: "public", label: "公开" }]} /></label>
        {editor.item && <label className="resource-switch-row">启用<Switch aria-label="启用 Tool" checked={editor.enabled} disabled={editor.kind === "script" && !editor.item.verified} onChange={(enabled) => update({ enabled })} /></label>}
        {editor.kind === "script" ? <>
          <label>语言<Select aria-label="脚本语言" style={{ width: "100%" }} value={editor.language} disabled={Boolean(editor.item)} onChange={(language) => update({ language, requirements: language === "bash" ? "" : editor.requirements })}
            options={[{ value: "python", label: "Python" }, { value: "bash", label: "Bash" }]} /></label>
          <label htmlFor="tool-schema">输入结构（JSON Schema）<Input.TextArea id="tool-schema" aria-label="输入结构（JSON Schema）" value={editor.schema} rows={7} spellCheck={false} className="resource-code" onChange={(event) => update({ schema: event.target.value })} /></label>
          {editor.language === "python" && <>
            <label htmlFor="tool-requirements">Python 依赖（每行一个包名或版本约束）<Input.TextArea id="tool-requirements" aria-label="Python 依赖（每行一个包名或版本约束）" value={editor.requirements} rows={4} spellCheck={false}
              placeholder={"例如：httpx>=0.27,<1\npandas==2.2.3"} onChange={(event) => update({ requirements: event.target.value })} /></label>
            <label>导入 requirements.txt<Input type="file" accept=".txt" onChange={(event) => {
              const file = event.target.files?.[0]; if (file) void importRequirements(file); event.target.value = "";
            }} /></label>
            <Typography.Text type="secondary">保存后按依赖清单准备环境，完成后才能试运行。离线部署需由维护者补充相应安装包。</Typography.Text>
            {editor.item?.environment && <Alert showIcon
              type={environmentState === "failed" ? "error" : environmentState === "ready" ? "success" : "info"}
              message={`已保存版本 v${editor.item.current_version}：${environmentState === "pending" ? "依赖环境等待准备" : environmentState === "building" ? "正在准备依赖环境"
                : environmentState === "ready" ? "依赖环境已准备，可进行试运行" : environmentState === "failed" ? "依赖环境准备失败" : "无需额外依赖"}`}
              description={<Space direction="vertical">
                {editor.item.environment.message && <span>{editor.item.environment.message}</span>}
                {editor.item.environment.locked_requirements.length > 0 && <span>已固定版本：{editor.item.environment.locked_requirements.map((item) => `${item.name}==${item.version}`).join("、")}</span>}
                {environmentState === "failed" && <Button loading={retrying} onClick={() => void retryEnvironment()}>重试准备环境</Button>}
                {environmentState === "ready" && <Popconfirm title="重新准备会创建私有、停用的新版本，需要重新试运行。" onConfirm={() => void retryEnvironment()}>
                  <Button size="small" loading={retrying}>重新准备环境</Button>
                </Popconfirm>}
                {environmentPending && <span>页面会自动更新进度；可以关闭页面稍后查看。</span>}
              </Space>} />}
          </>}
          <label htmlFor="tool-source">脚本<Input.TextArea id="tool-source" aria-label="脚本" value={editor.source} rows={14} spellCheck={false} className="resource-code" onChange={(event) => update({ source: event.target.value })} /></label>
          <label>导入脚本文件<Input type="file" accept=".py,.sh" onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void importScript(file);
            event.target.value = "";
          }} /></label>
          {editor.item && <>
            <label htmlFor="tool-test-args">试运行参数（JSON）<Input.TextArea id="tool-test-args" aria-label="试运行参数（JSON）" value={testArgs} rows={4} spellCheck={false} className="resource-code" onChange={(event) => setTestArgs(event.target.value)} /></label>
            <Button icon={<FlaskConical size={16} />} loading={testing} disabled={Boolean(editor.item.environment && !["ready", "not_required"].includes(editor.item.environment.state))} onClick={() => void test()}>试运行</Button>
          </>}
        </> : <>
          <label htmlFor="tool-url">MCP 地址<Input id="tool-url" value={editor.url} placeholder="https://example.com/mcp" onChange={(event) => update({ url: event.target.value })} /></label>
          <label htmlFor="tool-token">Bearer 凭据<Input.Password id="tool-token" value={editor.bearerToken} placeholder={editor.item?.has_credentials ? "留空保留当前凭据" : undefined}
            onChange={(event) => update({ bearerToken: event.target.value })} /></label>
          <Button icon={<Search size={16} />} loading={discovering} onClick={() => void discover()}>发现工具</Button>
          <label>远端工具<Select aria-label="远端工具" style={{ width: "100%" }} showSearch value={editor.remoteName || undefined}
            onChange={(remoteName) => update({ remoteName })} options={discovered.map((item) => ({ value: item.name, label: item.name }))}
            placeholder={editor.remoteName || "选择发现的工具"} /></label>
          {editor.visibility === "public" && <label className="resource-switch-row">允许其他用户使用此连接凭据<Switch aria-label="共享连接凭据" checked={editor.shareCredentials} onChange={(shareCredentials) => update({ shareCredentials })} /></label>}
        </>}
      </Space>}
    </Drawer>
    <Modal title="试运行结果" open={testResult !== null} footer={<Button onClick={() => setTestResult(null)}>关闭</Button>}
      onCancel={() => setTestResult(null)}><pre className="resource-result">{testResult}</pre></Modal>
  </div>;
}
