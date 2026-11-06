import { useEffect, useMemo, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Divider,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Pagination,
  Popconfirm,
  Segmented,
  Select,
  Skeleton,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from "antd";
import type { ColumnsType } from "antd/es/table";
import { ArrowUpRight, Bot, LayoutGrid, List as ListIcon, MessageSquareText, Pencil, Plus, RefreshCw, Search, Trash2, UserPlus, UserRoundX } from "lucide-react";
import { useNavigate } from "react-router-dom";
import {
  ApiError,
  createAgent,
  deleteAgent,
  getAgent,
  getSharedAgent,
  grantAgentShare,
  listAgents,
  listAgentModels,
  listAgentShares,
  listSharedAgents,
  listSkills,
  listToolCatalog,
  revokeAgentShare,
  searchShareTargets,
  updateAgent,
  type Agent,
  type AgentDraft,
  type AgentPage,
  type AgentModelOption,
  type AgentShare,
  type SharedAgent,
  type Skill,
  type UserTool,
} from "./api";
import { listAllKnowledgeBases, type KnowledgeBase } from "./knowledge-api";
import { PageHeader } from "./components/ResourcePage";
import "./styles/agents.css";

type Tab = "my" | "shared";
type View = "card" | "table";
type DirectoryAgent = Agent | SharedAgent;
type Detail = { kind: Tab; agent: DirectoryAgent };

function isOwned(agent: DirectoryAgent): agent is Agent {
  return "default_model_id" in agent;
}

function modelLabel(id: number, models: AgentModelOption[]): string {
  const model = models.find((item) => item.id === id);
  return model?.display_name || `模型 #${id}`;
}

function AgentCard({
  agent,
  models,
  onView,
  onEdit,
  onSessions,
}: {
  agent: DirectoryAgent;
  models: AgentModelOption[];
  onView: () => void;
  onEdit?: () => void;
  onSessions: () => void;
}) {
  return (
    <Card className="agent-card" size="small">
      <div className="agent-card-head">
        <div className="agent-avatar"><Bot size={23} aria-hidden /></div>
        <div className="agent-card-title-group">
          <div className="agent-card-title" title={agent.name}>{agent.name}</div>
          <Tag color={agent.enabled ? "success" : "default"}>{agent.enabled ? "已启用" : "已停用"}</Tag>
        </div>
      </div>
      <p className="agent-card-description">{agent.description || "暂无描述"}</p>
      <div className="agent-card-meta">
        {isOwned(agent) ? modelLabel(agent.default_model_id, models) : `由用户 #${agent.owner_id} 共享`}
      </div>
      <div className="agent-card-actions">
        <Button type="link" icon={<MessageSquareText size={15} />} onClick={onSessions}>会话</Button>
        <Button type="text" icon={<ArrowUpRight size={15} />} onClick={onView}>查看详情</Button>
        {onEdit && <Button type="text" icon={<Pencil size={15} />} onClick={onEdit}>编辑</Button>}
      </div>
    </Card>
  );
}

function AgentEditor({
  agent,
  models,
  modelError,
  canSelectModel,
  open,
  onClose,
  onSaved,
}: {
  agent: Agent | null;
  models: AgentModelOption[];
  modelError: string;
  canSelectModel: boolean;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [form] = Form.useForm<AgentDraft>();
  const [saving, setSaving] = useState(false);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [tools, setTools] = useState<UserTool[]>([]);
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [knowledgeError, setKnowledgeError] = useState("");
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [error, setError] = useState("");
  const usableModels = models;
  const selectedModelId = Form.useWatch("default_model_id", form);
  const selectedModel = models.find((model) => model.id === selectedModelId);
  const currentModel = agent && !usableModels.some((model) => model.id === agent.default_model_id)
    ? [{ value: agent.default_model_id, label: modelLabel(agent.default_model_id, models) }]
    : [];

  useEffect(() => {
    if (!open) return;
    let active = true;
    setKnowledgeLoading(true); setKnowledgeError("");
    listAllKnowledgeBases().then((rows) => { if (active) setKnowledgeBases(rows); }).catch((failure: unknown) => {
      if (active) setKnowledgeError(failure instanceof Error ? failure.message : "知识库目录加载失败");
    }).finally(() => { if (active) setKnowledgeLoading(false); });
    listSkills().then(setSkills).catch(() => setSkills([]));
    listToolCatalog().then((catalog) => setTools(catalog.user_tools)).catch(() => setTools([]));
    setError("");
    form.setFieldsValue(agent ? {
      name: agent.name,
      description: agent.description,
      system_prompt: agent.system_prompt,
      welcome_message: agent.welcome_message,
      default_model_id: agent.default_model_id,
      temperature: agent.temperature,
      max_output_tokens: agent.max_output_tokens,
      model_timeout_seconds: agent.model_timeout_seconds,
      tool_timeout_seconds: agent.tool_timeout_seconds,
      max_model_calls: agent.max_model_calls,
      max_tool_calls: agent.max_tool_calls,
      max_total_tokens: agent.max_total_tokens,
      max_estimated_cost: agent.max_estimated_cost,
      enabled: agent.enabled,
      skill_ids: agent.skill_ids,
      tool_ids: agent.tool_ids,
      knowledge_base_ids: agent.knowledge_base_ids || [],
    } : {
      name: "",
      description: null,
      system_prompt: "",
      welcome_message: null,
      default_model_id: undefined,
      temperature: "1",
      max_output_tokens: 4096,
      model_timeout_seconds: 120,
      tool_timeout_seconds: 120,
      max_model_calls: null,
      max_tool_calls: null,
      max_total_tokens: null,
      max_estimated_cost: null,
      enabled: true,
      skill_ids: [],
      tool_ids: [],
      knowledge_base_ids: [],
    });
    return () => { active = false; };
  }, [agent, form, open]);

  async function save(values: AgentDraft) {
    setSaving(true);
    setError("");
    try {
      if (agent) {
        const changes: Partial<AgentDraft> = {};
        if (values.name !== agent.name) changes.name = values.name;
        if ((values.description || null) !== agent.description) changes.description = values.description || null;
        if (values.system_prompt !== agent.system_prompt) changes.system_prompt = values.system_prompt;
        if ((values.welcome_message || null) !== agent.welcome_message) changes.welcome_message = values.welcome_message || null;
        if (values.enabled !== agent.enabled) changes.enabled = values.enabled;
        if (JSON.stringify(values.skill_ids || []) !== JSON.stringify(agent.skill_ids || [])) changes.skill_ids = values.skill_ids || [];
        if (JSON.stringify(values.tool_ids || []) !== JSON.stringify(agent.tool_ids || [])) changes.tool_ids = values.tool_ids || [];
        if (JSON.stringify(values.knowledge_base_ids || []) !== JSON.stringify(agent.knowledge_base_ids || [])) changes.knowledge_base_ids = values.knowledge_base_ids || [];
        if (canSelectModel && values.default_model_id !== agent.default_model_id) changes.default_model_id = values.default_model_id;
        if (Number(values.temperature) !== Number(agent.temperature)) changes.temperature = values.temperature;
        if (values.max_output_tokens !== agent.max_output_tokens) changes.max_output_tokens = values.max_output_tokens;
        if (values.model_timeout_seconds !== agent.model_timeout_seconds) changes.model_timeout_seconds = values.model_timeout_seconds;
        if (values.tool_timeout_seconds !== agent.tool_timeout_seconds) changes.tool_timeout_seconds = values.tool_timeout_seconds;
        if ((values.max_model_calls ?? null) !== agent.max_model_calls) changes.max_model_calls = values.max_model_calls ?? null;
        if ((values.max_tool_calls ?? null) !== agent.max_tool_calls) changes.max_tool_calls = values.max_tool_calls ?? null;
        if ((values.max_total_tokens ?? null) !== agent.max_total_tokens) changes.max_total_tokens = values.max_total_tokens ?? null;
        if ((values.max_estimated_cost == null ? null : Number(values.max_estimated_cost)) !== (agent.max_estimated_cost == null ? null : Number(agent.max_estimated_cost))) changes.max_estimated_cost = values.max_estimated_cost ?? null;
        if (Object.keys(changes).length === 0) { onClose(); return; }
        await updateAgent(agent.id, agent.version, changes);
      } else {
        if (!values.default_model_id) return;
        await createAgent({ ...values, default_model_id: values.default_model_id });
      }
      message.success(agent ? "Agent 已更新" : "Agent 已创建");
      onSaved();
    } catch (failure) {
      const text = failure instanceof Error ? failure.message : "保存失败";
      setError(failure instanceof ApiError && failure.status === 409 ? `${text}，请刷新列表后重新编辑` : text);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Drawer
      title={agent ? "编辑 Agent" : "新建 Agent"}
      open={open}
      onClose={onClose}
      width="min(100vw, var(--yp-detail-width))"
      extra={<Button type="primary" loading={saving} onClick={() => form.submit()}>保存</Button>}
    >
      {(error || (canSelectModel && modelError)) && <Alert className="drawer-alert" type="error" showIcon message={error || modelError} />}
      {!agent && !modelError && usableModels.length === 0 && (
        <Alert className="drawer-alert" type="info" showIcon message="暂无已验证文本能力的可用模型" />
      )}
      <Form<AgentDraft> form={form} layout="vertical" onFinish={save} requiredMark={false}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: "请输入名称" }]}>
          <Input maxLength={128} showCount />
        </Form.Item>
        <Form.Item name="description" label="描述"><Input.TextArea rows={3} maxLength={1024} showCount /></Form.Item>
        {canSelectModel ? (
          <Form.Item name="default_model_id" label="默认模型" rules={[{ required: true, message: "请选择模型" }]}>
            <Select
              disabled={Boolean(agent && usableModels.length === 0)}
              options={[...currentModel, ...usableModels.map((model) => ({ value: model.id, label: model.display_name }))]}
              placeholder="选择已验证文本能力的模型"
            />
          </Form.Item>
        ) : agent && <Form.Item label="默认模型"><Typography.Text>{modelLabel(agent.default_model_id, models)}</Typography.Text></Form.Item>}
        <Form.Item name="system_prompt" label="系统指令"><Input.TextArea rows={5} maxLength={32768} /></Form.Item>
        <Form.Item name="skill_ids" label="Skill">
          <Select mode="multiple" maxCount={16} optionFilterProp="label" placeholder="选择已发布的 Skill（关联固定版本）"
            options={skills.filter((item) => item.enabled && item.current_version > 0).map((item) => ({ value: item.id, label: `${item.name} · v${item.current_version} · ${item.description}` }))} />
        </Form.Item>
        <Form.Item name="tool_ids" label="Tool">
          <Select mode="multiple" maxCount={16} optionFilterProp="label" placeholder="选择可用 Tool"
            options={tools.filter((item) => item.enabled).map((item) => ({ value: item.id, label: `${item.name} · ${item.description}` }))} />
        </Form.Item>
        {knowledgeError && <Alert className="drawer-alert" type="warning" showIcon message={`知识库目录加载失败：${knowledgeError}`} description="保留原有关联，关闭后重新打开编辑器可重试加载。" />}
        <Form.Item name="knowledge_base_ids" label="知识库" extra="最多 8 个；Agent 共享不会扩大知识库权限，接收方需要单独授权。未发布索引的资料暂不参与检索。">
          <Select mode="multiple" maxCount={8} loading={knowledgeLoading} disabled={knowledgeLoading || Boolean(knowledgeError)} optionFilterProp="label" placeholder="关联可访问的知识库"
            options={[...knowledgeBases.map((item) => ({ value: item.id, label: `${item.name}${item.active_index_id === null ? " · 未发布索引" : ""}` })), ...(agent?.knowledge_base_ids || []).filter((id) => !knowledgeBases.some((item) => item.id === id)).map((id) => ({ value: id, label: `知识库 #${id} · 当前不可访问` }))]} />
        </Form.Item>
        <Form.Item name="welcome_message" label="欢迎语"><Input.TextArea rows={3} maxLength={2048} /></Form.Item>
        <Collapse ghost items={[{ key: "runtime", label: "运行设置", forceRender: true, children: <>
          <Form.Item name="temperature" label="生成温度" rules={[{ required: true }]}><InputNumber min={0} max={2} step={0.1} precision={2} stringMode className="agent-number-input" /></Form.Item>
          <Form.Item name="max_output_tokens" label="最大输出 Token" rules={[{ required: true, type: "number", min: 1, max: 65536 }, { validator: async (_, value) => {
            if (selectedModel?.context_window && Number(value) + 1024 > selectedModel.context_window) throw new Error("输出上限需为上下文保留至少 1024 Token");
          } }]}><InputNumber min={1} max={65536} precision={0} className="agent-number-input" /></Form.Item>
          <div className="agent-form-pair">
            <Form.Item name="model_timeout_seconds" label="模型超时（秒）" rules={[{ required: true }]}><InputNumber min={1} max={120} precision={0} className="agent-number-input" /></Form.Item>
            <Form.Item name="tool_timeout_seconds" label="工具超时（秒）" rules={[{ required: true }]}><InputNumber min={1} max={120} precision={0} className="agent-number-input" /></Form.Item>
          </div>
          <div className="agent-form-pair">
            <Form.Item name="max_model_calls" label="模型调用上限"><InputNumber min={1} max={100} precision={0} className="agent-number-input" /></Form.Item>
            <Form.Item name="max_tool_calls" label="工具调用上限"><InputNumber min={1} max={100} precision={0} className="agent-number-input" /></Form.Item>
          </div>
          <Form.Item name="max_total_tokens" label="每轮总 Token 上限"><InputNumber min={1} max={10000000} precision={0} className="agent-number-input" /></Form.Item>
          <Form.Item name="max_estimated_cost" label="每轮估算成本上限" extra="按模型配置的价格估算；未设置价格的模型不能启用此上限"><InputNumber min={0.000001} precision={6} stringMode className="agent-number-input" /></Form.Item>
        </> }]} />
        <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
      </Form>
    </Drawer>
  );
}

function AgentShares({ agentId }: { agentId: number }) {
  const [shares, setShares] = useState<AgentShare[]>([]);
  const [targets, setTargets] = useState<AgentShare[]>([]);
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<number>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    listAgentShares(agentId).then((items) => { if (active) setShares(items); }).catch((failure) => { if (active) setError(failure instanceof Error ? failure.message : "授权列表加载失败"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [agentId]);

  useEffect(() => {
    if (query.trim().length < 2) { setTargets([]); return; }
    let active = true;
    const timer = window.setTimeout(() => {
      searchShareTargets(query.trim()).then((items) => { if (active) setTargets(items.filter((item) => !shares.some((share) => share.user_id === item.user_id))); }).catch((failure) => { if (active) setError(failure instanceof Error ? failure.message : "用户搜索失败"); });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query, shares]);

  async function grant() {
    if (!selectedId) return;
    setBusy(true);
    setError("");
    try {
      const share = await grantAgentShare(agentId, selectedId);
      setShares((current) => current.some((item) => item.user_id === share.user_id) ? current : [...current, share]);
      setSelectedId(undefined);
      setQuery("");
      message.success("已授权");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "授权失败"); }
    finally { setBusy(false); }
  }

  async function revoke(userId: number) {
    setBusy(true);
    setError("");
    try {
      await revokeAgentShare(agentId, userId);
      setShares((current) => current.filter((item) => item.user_id !== userId));
      message.success("授权已撤销");
    } catch (failure) { setError(failure instanceof Error ? failure.message : "撤销失败"); }
    finally { setBusy(false); }
  }

  return <div className="agent-shares">
    <Divider orientation="left">共享授权</Divider>
    {error && <Alert className="drawer-alert" type="error" showIcon message={error} />}
    <div className="agent-share-form">
      <Select aria-label="搜索用户名" showSearch filterOption={false} value={selectedId} searchValue={query} onSearch={setQuery} onChange={setSelectedId} placeholder="搜索用户名" options={targets.map((item) => ({ value: item.user_id, label: item.username }))} notFoundContent={query.length < 2 ? "输入至少 2 个字符" : "没有可选用户"} />
      <Button icon={<UserPlus size={16} />} loading={busy} disabled={!selectedId} onClick={() => void grant()}>授权</Button>
    </div>
    {loading ? <Skeleton active paragraph={{ rows: 2 }} /> : shares.length ? <List dataSource={shares} renderItem={(share) => <List.Item actions={[<Popconfirm key="revoke" title={`撤销 ${share.username} 的使用权限？`} description="该用户正在运行的任务会请求取消。" okText="撤销" cancelText="取消" onConfirm={() => void revoke(share.user_id)}><Button type="text" danger icon={<UserRoundX size={16} />} aria-label={`撤销授权 ${share.username}`} disabled={busy} /></Popconfirm>]}><List.Item.Meta title={share.username} description={`用户 #${share.user_id}`} /></List.Item>} /> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未共享" />}
  </div>;
}

export default function Agents() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("my");
  const [view, setView] = useState<View>("card");
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [revision, setRevision] = useState(0);
  const [owned, setOwned] = useState<AgentPage>({ items: [], total: 0, page: 1, page_size: 20 });
  const [shared, setShared] = useState<SharedAgent[]>([]);
  const [models, setModels] = useState<AgentModelOption[]>([]);
  const [modelError, setModelError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<Detail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [editing, setEditing] = useState<Agent | "create" | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([listAgents(page), listSharedAgents()])
      .then(([mine, given]) => {
        if (!active) return;
        setOwned(mine);
        setShared(given);
      })
      .catch((failure) => {
        if (active) setError(failure instanceof Error ? failure.message : "Agent 列表加载失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [page, revision]);

  useEffect(() => {
    setModelError("");
    listAgentModels()
      .then(setModels)
      .catch((failure) => setModelError(failure instanceof Error ? failure.message : "模型列表加载失败"));
  }, [revision]);

  const items: DirectoryAgent[] = tab === "my" ? owned.items : shared;
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    if (!needle) return items;
    return items.filter((agent) => `${agent.name} ${agent.description || ""}`.toLocaleLowerCase().includes(needle));
  }, [items, query]);

  async function openDetail(agent: DirectoryAgent) {
    const kind = isOwned(agent) ? "my" : "shared";
    setDetail({ kind, agent });
    setDetailLoading(true);
    setDetailError("");
    try {
      const current = kind === "my" ? await getAgent(agent.id) : await getSharedAgent(agent.id);
      setDetail({ kind, agent: current });
    } catch (failure) {
      setDetailError(failure instanceof Error ? failure.message : "详情加载失败");
      if (failure instanceof ApiError && failure.status === 404) refresh();
    } finally {
      setDetailLoading(false);
    }
  }

  function refresh() {
    setRevision((value) => value + 1);
  }

  async function removeAgent(agent: Agent) {
    setDeleting(true);
    setDetailError("");
    try {
      await deleteAgent(agent.id);
      setDetail(null);
      message.success("Agent 已删除");
      refresh();
    } catch (failure) { setDetailError(failure instanceof Error ? failure.message : "删除失败"); }
    finally { setDeleting(false); }
  }

  const columns: ColumnsType<DirectoryAgent> = [
    {
      title: "Agent",
      key: "name",
      render: (_, agent) => <div className="agent-table-name"><Bot size={19} aria-hidden /><span>{agent.name}</span></div>,
    },
    { title: "描述", dataIndex: "description", key: "description", render: (value: string | null) => value || "暂无描述", ellipsis: true },
    { title: "状态", key: "status", width: 100, render: (_, agent) => <Tag color={agent.enabled ? "success" : "default"}>{agent.enabled ? "已启用" : "已停用"}</Tag> },
    { title: tab === "my" ? "默认模型" : "所有者", key: "owner", width: 160, render: (_, agent) => isOwned(agent) ? modelLabel(agent.default_model_id, models) : `用户 #${agent.owner_id}` },
    { title: "操作", key: "actions", width: 140, render: (_, agent) => (
      <Space size="small">
        <Button type="link" size="small" onClick={() => navigate(`/agents/${agent.id}/sessions`)}>会话</Button>
        <Button type="link" size="small" onClick={() => void openDetail(agent)}>查看</Button>
        {isOwned(agent) && <Button type="link" size="small" onClick={() => setEditing(agent)}>编辑</Button>}
      </Space>
    ) },
  ];

  return (
    <div className="workspace agent-page">
      <PageHeader title="Agent" description="配置专属助手，连接模型、知识与工具，开始协作。" actions={<Button type="primary" icon={<Plus size={17} />} onClick={() => setEditing("create")}>新建 Agent</Button>} />

      <div className="agent-page-surface">
        <Tabs
          className="agent-tabs"
          activeKey={tab}
          onChange={(key) => { setTab(key as Tab); setQuery(""); }}
          items={[
            { key: "my", label: `我的 Agent (${owned.total})` },
            { key: "shared", label: `共享给我 (${shared.length})` },
          ]}
        />
        <div className="agent-toolbar">
          <div className="agent-count">{tab === "my" ? `共 ${owned.total} 个` : `共 ${shared.length} 个`}</div>
          <div className="agent-toolbar-actions">
            <Input
              className="agent-search"
              aria-label="搜索 Agent"
              prefix={<Search size={16} />}
              placeholder={tab === "my" ? "筛选当前页" : "搜索共享 Agent"}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              allowClear
            />
            <Segmented
              value={view}
              onChange={(value) => setView(value as View)}
              options={[
                { value: "card", label: <span className="view-option"><LayoutGrid size={15} />卡片</span> },
                { value: "table", label: <span className="view-option"><ListIcon size={15} />表格</span> },
              ]}
            />
            <Tooltip title="刷新列表"><Button icon={<RefreshCw size={17} />} aria-label="刷新列表" onClick={refresh} /></Tooltip>
          </div>
        </div>

        {error && <Alert className="agent-list-alert" type="error" showIcon message={error} action={<Button size="small" onClick={refresh}>重试</Button>} />}
        {loading ? <div className="agent-grid"><Skeleton active /><Skeleton active /><Skeleton active /></div> : !error && filtered.length === 0 ? (
          <div className="agent-empty"><Empty description={query ? "没有符合条件的 Agent" : tab === "my" ? "还没有 Agent" : "还没有共享给你的 Agent"} /></div>
        ) : !error && view === "card" ? (
          <div className="agent-grid">
            {filtered.map((agent) => <AgentCard key={agent.id} agent={agent} models={models} onView={() => void openDetail(agent)} onSessions={() => navigate(`/agents/${agent.id}/sessions`)} onEdit={isOwned(agent) ? () => setEditing(agent) : undefined} />)}
          </div>
        ) : !error && (
          <Table<DirectoryAgent>
            className="agent-table"
            rowKey="id"
            columns={columns}
            dataSource={filtered}
            pagination={false}
            scroll={{ x: "max-content" }}
          />
        )}
        {!loading && !error && tab === "my" && owned.total > owned.page_size && (
          <Pagination className="agent-pagination" current={page} pageSize={owned.page_size} total={owned.total} onChange={setPage} showSizeChanger={false} />
        )}
      </div>

      <Drawer
        title="Agent 详情"
        open={Boolean(detail)}
        onClose={() => setDetail(null)}
        width="min(100vw, var(--yp-detail-width))"
        extra={detail ? <Space><Button icon={<MessageSquareText size={16} />} onClick={() => navigate(`/agents/${detail.agent.id}/sessions`)}>会话</Button>{detail.kind === "my" && isOwned(detail.agent) && <><Button icon={<Pencil size={16} />} onClick={() => { setEditing(detail.agent as Agent); setDetail(null); }}>编辑</Button><Popconfirm title={`删除 ${detail.agent.name}？`} description="共享授权会撤销，已有会话无法继续使用。" okText="删除" cancelText="取消" onConfirm={() => void removeAgent(detail.agent as Agent)}><Button danger loading={deleting} icon={<Trash2 size={16} />} aria-label="删除 Agent" /></Popconfirm></>}</Space> : undefined}
      >
        {detailError && <Alert type="error" showIcon message={detailError} className="drawer-alert" />}
        {detailLoading ? <Skeleton active /> : detail && !detailError && (
          <Descriptions column={1} size="small" bordered items={[
            { key: "name", label: "名称", children: detail.agent.name },
            { key: "description", label: "描述", children: detail.agent.description || "暂无描述" },
            { key: "status", label: "状态", children: detail.agent.enabled ? "已启用" : "已停用" },
            ...(isOwned(detail.agent) ? [
              { key: "model", label: "默认模型", children: modelLabel(detail.agent.default_model_id, models) },
              { key: "prompt", label: "系统指令", children: <div className="detail-long-text">{detail.agent.system_prompt || "未设置"}</div> },
              { key: "temperature", label: "生成温度", children: detail.agent.temperature ?? "1" },
              { key: "output", label: "最大输出", children: `${detail.agent.max_output_tokens ?? 4096} Token` },
              { key: "timeouts", label: "模型 / 工具超时", children: `${detail.agent.model_timeout_seconds ?? 120} / ${detail.agent.tool_timeout_seconds ?? 120} 秒` },
              { key: "budget", label: "每轮预算", children: `模型 ${detail.agent.max_model_calls ?? "不限"} 次 · 工具 ${detail.agent.max_tool_calls ?? "不限"} 次 · Token ${detail.agent.max_total_tokens ?? "不限"} · 成本 ${detail.agent.max_estimated_cost ?? "不限"}` },
            ] : [{ key: "owner", label: "所有者", children: `用户 #${detail.agent.owner_id}` }]),
            { key: "welcome", label: "欢迎语", children: detail.agent.welcome_message || "未设置" },
          ]} />
        )}
        {detail?.kind === "my" && !detailLoading && !detailError && <AgentShares key={detail.agent.id} agentId={detail.agent.id} />}
      </Drawer>

      <AgentEditor
        agent={editing === "create" ? null : editing}
        models={models}
        modelError={modelError}
        canSelectModel
        open={editing !== null}
        onClose={() => setEditing(null)}
        onSaved={() => { setEditing(null); setTab("my"); setPage(1); refresh(); }}
      />
    </div>
  );
}
