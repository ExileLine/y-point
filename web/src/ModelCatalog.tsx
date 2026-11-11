import { useRuntimeLimits } from "./runtime-limits";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Checkbox, Drawer, Dropdown, Empty, Form, Input, InputNumber, Modal, Select, Skeleton, Space, Switch, Table, Tabs, Tag, Tooltip, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { Check, CheckCircle2, MoreHorizontal, Pencil, Plus, RefreshCw, Search, Star, Trash2, Unplug } from "lucide-react";
import {
  createModel, createProvider, deleteModel, deleteProvider, discoverModels, importModels, listModels, listProviderKinds,
  listProviders, setDefaultModel, testModel, testModelCapability, testProvider, updateModel, updateProvider,
  type DiscoveryResult, type Model, type ModelDraft, type Provider, type ProviderDraft, type ProviderKind,
} from "./api";
import { PageHeader } from "./components/ResourcePage";
import "./styles/model-catalog.css";

const capabilities = [
  { value: "text", label: "文本" }, { value: "streaming", label: "流式" }, { value: "tools", label: "工具" },
  { value: "structured_output", label: "结构化输出" }, { value: "vision", label: "视觉" }, { value: "reasoning_summary", label: "推理摘要" },
];
function CapabilityTags({ model }: { model: Model }) {
  return <div className="catalog-capabilities">{model.capabilities.length ? model.capabilities.map((capability) => {
    const verified = model.verified_capabilities.includes(capability);
    const label = capabilities.find((item) => item.value === capability)?.label || capability;
    return <Tooltip key={capability} title={`${label}：${verified ? "已验证" : "待验证"}`}><Tag className={`capability-tag capability-${capability}`} data-verified={verified}>{verified && <Check size={12} aria-hidden="true" />}{label}</Tag></Tooltip>;
  }) : "待配置"}</div>;
}

const fieldLabels: Record<string, string> = {
  name: "连接名称", base_url: "接口地址", api_key: "API 密钥", custom_headers: "自定义请求头",
  api_version: "接口版本", region: "服务区域", timeout_seconds: "超时（秒）", remark: "备注", status: "启用",
};
const failureLabels: Record<string, string> = {
  authentication: "凭据无效", rate_limit: "上游限流", model_list_unavailable: "模型列表不可用",
  model_not_found: "模型不存在", invalid_request: "请求无效", timeout: "连接超时", network: "网络故障",
  upstream: "上游故障", invalid_response: "响应无效", unsupported_capability: "能力不受支持",
};

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

type ProviderValues = ProviderDraft & { custom_headers_text?: string; enabled: boolean };

function ProviderEditor({ open, provider, kinds, onClose, onSaved }: {
  open: boolean; provider: Provider | null; kinds: ProviderKind[]; onClose: () => void; onSaved: () => void;
}) {
  const [form] = Form.useForm<ProviderValues>();
  const { limits, error: limitsError, retry: retryLimits } = useRuntimeLimits(open);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const kind = Form.useWatch("kind", form);
  const spec = kinds.find((item) => item.kind === kind);
  useEffect(() => {
    if (!open) return;
    setError("");
    form.resetFields();
    form.setFieldsValue(provider ? {
      name: provider.name, kind: provider.kind, base_url: provider.base_url, api_version: provider.api_version,
      region: provider.region, timeout_seconds: provider.timeout_seconds, remark: provider.remark,
      enabled: provider.status === 1,
    } : { kind: kinds[0]?.kind, timeout_seconds: 15, enabled: true });
  }, [form, kinds, open, provider]);

  async function save(values: ProviderValues) {
    setSaving(true);
    setError("");
    try {
      let headers: Record<string, string> | undefined;
      if (values.custom_headers_text?.trim()) {
        const parsed: unknown = JSON.parse(values.custom_headers_text);
        if (!parsed || Array.isArray(parsed) || typeof parsed !== "object" || Object.values(parsed).some((item) => typeof item !== "string")) {
          throw new Error("自定义请求头须为 JSON 对象，值须为字符串");
        }
        headers = parsed as Record<string, string>;
      }
      const draft: ProviderDraft = {
        name: values.name, base_url: values.base_url || undefined, api_version: values.api_version || undefined,
        region: values.region || undefined, timeout_seconds: values.timeout_seconds, remark: values.remark || undefined,
        status: values.enabled ? 1 : 0,
        ...(values.api_key ? { api_key: values.api_key } : {}),
        ...(headers ? { custom_headers: headers } : {}),
      };
      if (provider) {
        const changes: Partial<ProviderDraft> = {};
        if (draft.name !== provider.name) changes.name = draft.name;
        if (draft.base_url !== provider.base_url) changes.base_url = draft.base_url;
        if (draft.api_version !== (provider.api_version || undefined)) changes.api_version = draft.api_version;
        if (draft.region !== (provider.region || undefined)) changes.region = draft.region;
        if (draft.timeout_seconds !== provider.timeout_seconds) changes.timeout_seconds = draft.timeout_seconds;
        if (draft.remark !== (provider.remark || undefined)) changes.remark = draft.remark;
        if (draft.status !== provider.status) changes.status = draft.status;
        if (draft.api_key) changes.api_key = draft.api_key;
        if (draft.custom_headers) changes.custom_headers = draft.custom_headers;
        if (Object.keys(changes).length === 0) { onSaved(); return; }
        await updateProvider(provider.id, changes);
      }
      else await createProvider({ ...draft, kind: values.kind });
      message.success(provider ? "连接已更新" : "连接已创建");
      onSaved();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setSaving(false);
    }
  }

  function field(name: string) {
    const required = spec?.required_fields.includes(name) && !(provider && name === "api_key" && provider.has_api_key);
    if (name === "status") return <Form.Item key={name} name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>;
    if (name === "timeout_seconds") return <Form.Item key={name} name={name} label={fieldLabels[name]} extra="必填，新连接通常默认 15 秒。独立于 Agent 单次模型超时；长时间思考或等待首包时，应同步调整此值。" rules={[{ required: true }]}><InputNumber min={1} max={limits?.max_model_timeout_seconds} className="catalog-full-input" /></Form.Item>;
    if (name === "remark") return <Form.Item key={name} name={name} label={fieldLabels[name]}><Input.TextArea rows={2} maxLength={512} /></Form.Item>;
    if (name === "custom_headers") return <Form.Item key={name} name="custom_headers_text" label={fieldLabels[name]} extra={provider?.has_custom_headers ? "已配置；留空保留原值" : "JSON 对象，例如 {\"X-Tenant\":\"tenant\"}"}><Input.TextArea rows={3} /></Form.Item>;
    return <Form.Item key={name} name={name} label={fieldLabels[name]} rules={required ? [{ required: true, whitespace: true, message: `请输入${fieldLabels[name]}` }] : undefined} extra={name === "api_key" && provider?.has_api_key ? "已配置；留空保留原值" : undefined}>
      <Input type={name === "api_key" ? "password" : "text"} autoComplete={name === "api_key" ? "new-password" : undefined} maxLength={name === "base_url" ? 512 : name === "name" ? 128 : 64} />
    </Form.Item>;
  }

  const fields = [...new Set([...(spec?.required_fields || []), ...(spec?.optional_fields || [])])].filter((name) => name !== "kind");
  return <Drawer title={provider ? "编辑供应商连接" : "新建供应商连接"} open={open} onClose={onClose} width="min(100vw, var(--yp-detail-width))" extra={<Button type="primary" disabled={!limits} loading={saving} onClick={() => form.submit()}>保存</Button>}>
    {limitsError && <Alert type="error" showIcon message={`运行配置加载失败：${limitsError}`} action={<Button size="small" onClick={retryLimits}>重试</Button>} />}
    {error && <Alert className="catalog-alert" type="error" showIcon message={error} />}
    <Form form={form} layout="vertical" requiredMark={false} onFinish={(values) => void save(values)}>
      <Form.Item name="kind" label="供应商类型" rules={[{ required: true }]}><Select disabled={Boolean(provider)} options={kinds.map((item) => ({ value: item.kind, label: item.display_name }))} onChange={(value) => { if (!provider) form.setFieldValue("base_url", kinds.find((item) => item.kind === value)?.suggested_base_url || undefined); }} /></Form.Item>
      {fields.map(field)}
    </Form>
  </Drawer>;
}

type ModelValues = Omit<ModelDraft, "provider_id" | "status"> & { enabled: boolean };

function ModelEditor({ open, model, providers, initialProvider, onClose, onSaved }: {
  open: boolean; model: Model | null; providers: Provider[]; initialProvider: number | undefined; onClose: () => void; onSaved: () => void;
}) {
  const [form] = Form.useForm<ModelValues & { provider_id: number }>();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    setError("");
    form.resetFields();
    form.setFieldsValue(model ? {
      provider_id: model.provider_id, external_id: model.external_id, display_name: model.display_name,
      capabilities: model.capabilities, context_window: model.context_window, input_price: model.input_price,
      output_price: model.output_price, price_currency: model.price_currency, enabled: model.status === 1,
    } : { provider_id: initialProvider, capabilities: ["text"], price_currency: "USD", enabled: true });
  }, [form, initialProvider, model, open]);

  async function save(values: ModelValues & { provider_id: number }) {
    setSaving(true);
    setError("");
    try {
      const draft: ModelDraft = {
        provider_id: values.provider_id, external_id: values.external_id, display_name: values.display_name,
        capabilities: values.capabilities || [], context_window: values.context_window || null,
        input_price: values.input_price || null, output_price: values.output_price || null,
        price_currency: values.price_currency, status: values.enabled ? 1 : 0,
      };
      if (model) {
        const changes: Partial<ModelDraft> = {};
        if (draft.external_id !== model.external_id) changes.external_id = draft.external_id;
        if (draft.display_name !== model.display_name) changes.display_name = draft.display_name;
        if (JSON.stringify(draft.capabilities) !== JSON.stringify(model.capabilities)) changes.capabilities = draft.capabilities;
        if (draft.context_window !== model.context_window) changes.context_window = draft.context_window;
        if (draft.input_price !== model.input_price) changes.input_price = draft.input_price;
        if (draft.output_price !== model.output_price) changes.output_price = draft.output_price;
        if (draft.price_currency !== model.price_currency) changes.price_currency = draft.price_currency;
        if (draft.status !== model.status) changes.status = draft.status;
        if (Object.keys(changes).length === 0) { onSaved(); return; }
        await updateModel(model.id, changes);
      } else await createModel(draft);
      message.success(model ? "模型已更新" : "模型已录入");
      onSaved();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setSaving(false);
    }
  }

  return <Drawer title={model ? "编辑模型" : "录入模型"} open={open} onClose={onClose} width="min(100vw, var(--yp-detail-width))" extra={<Button type="primary" loading={saving} onClick={() => form.submit()}>保存</Button>}>
    {error && <Alert className="catalog-alert" type="error" showIcon message={error} />}
    <Form form={form} layout="vertical" requiredMark={false} onFinish={(values) => void save(values)}>
      <Form.Item name="provider_id" label="供应商连接" rules={[{ required: true }]}><Select disabled={Boolean(model)} options={providers.map((item) => ({ value: item.id, label: item.name, disabled: item.status !== 1 }))} /></Form.Item>
      <Form.Item name="external_id" label="供应商模型 ID" extra="须与供应商实际开放的模型标识一致；显示名称可自行命名。" rules={[{ required: true, whitespace: true }]}><Input maxLength={255} /></Form.Item>
      <Form.Item name="display_name" label="显示名称" rules={[{ required: true, whitespace: true }]}><Input maxLength={128} /></Form.Item>
      <Form.Item name="capabilities" label="声明能力" extra="声明后仍需逐项验证；Agent 会检查已验证的文本能力，使用 Skill、Tool 或知识库还需要工具能力。"><Select mode="multiple" options={capabilities} /></Form.Item>
      <Form.Item name="context_window" label="上下文窗口（Token）" extra="填写当前部署实际允许的窗口，包含输入与输出。留空表示未知，平台无法据此可靠预检；调大此值不会扩容模型。"><InputNumber min={1} precision={0} className="catalog-full-input" /></Form.Item>
      <div className="catalog-form-row">
        <Form.Item name="input_price" label="输入价格 / 百万 Token" extra="留空表示价格未知；0 表示按零价格估算。与输出价格一起用于 Agent 成本预算。"><InputNumber min={0} precision={6} stringMode className="catalog-full-input" /></Form.Item>
        <Form.Item name="output_price" label="输出价格 / 百万 Token" extra="按下方币种、每百万 Token 填写；供应商单独计价的项目需核对，估算不等于实际账单。"><InputNumber min={0} precision={6} stringMode className="catalog-full-input" /></Form.Item>
      </div>
      <Form.Item name="price_currency" label="币种" rules={[{ required: true, pattern: /^[A-Z]{3}$/, message: "请输入三位大写币种代码" }]}><Input maxLength={3} /></Form.Item>
      <Form.Item name="enabled" label="启用" valuePropName="checked"><Switch /></Form.Item>
    </Form>
  </Drawer>;
}

export default function ModelCatalog() {
  const [tab, setTab] = useState("providers");
  const [providers, setProviders] = useState<Provider[]>([]);
  const [kinds, setKinds] = useState<ProviderKind[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [providerEditor, setProviderEditor] = useState<Provider | "new" | null>(null);
  const [modelEditor, setModelEditor] = useState<Model | "new" | null>(null);
  const [providerFilter, setProviderFilter] = useState<number | undefined>();
  const [query, setQuery] = useState("");
  const [discovery, setDiscovery] = useState<{ provider: Provider; result: DiscoveryResult } | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [testResult, setTestResult] = useState("");

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextProviders, nextKinds, nextModels] = await Promise.all([listProviders(), listProviderKinds(), listModels()]);
      setProviders(nextProviders);
      setKinds(nextKinds);
      setModels(nextModels);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => { void reload(); }, [reload]);

  async function perform(key: string, action: () => Promise<unknown>, success: string) {
    setBusy(key);
    setError("");
    setTestResult("");
    try {
      await action();
      message.success(success);
      await reload();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy("");
    }
  }

  async function probe(key: string, action: () => Promise<{ available: boolean; latency_ms: number; failure: string | null }>, name: string) {
    setBusy(key);
    setError("");
    try {
      const result = await action();
      setTestResult(`${name}：${result.available ? "可用" : failureLabels[result.failure || ""] || "不可用"} · ${result.latency_ms} ms`);
      await reload();
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy("");
    }
  }

  async function discover(provider: Provider) {
    setBusy(`discover-${provider.id}`);
    setError("");
    try {
      const result = await discoverModels(provider.id);
      setDiscovery({ provider, result });
      setSelected([]);
    } catch (failure) {
      setError(errorText(failure));
    } finally {
      setBusy("");
    }
  }

  const filteredModels = useMemo(() => models.filter((model) =>
    (providerFilter === undefined || model.provider_id === providerFilter) &&
    `${model.display_name} ${model.external_id}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  ), [models, providerFilter, query]);
  const providerName = (id: number) => providers.find((item) => item.id === id)?.name || `连接 #${id}`;
  const providerEnabled = (id: number) => providers.find((item) => item.id === id)?.status === 1;

  function providerActions(row: Provider) {
    return <Space size="small">
      <Tooltip title="发现模型"><Button aria-label={`发现模型 ${row.name}`} icon={<Search size={16} />} loading={busy === `discover-${row.id}`} onClick={() => void discover(row)} /></Tooltip>
      <Tooltip title="测试连接"><Button aria-label={`测试连接 ${row.name}`} icon={<Unplug size={16} />} loading={busy === `provider-${row.id}`} onClick={() => void probe(`provider-${row.id}`, () => testProvider(row.id), row.name)} /></Tooltip>
      <Tooltip title="编辑连接"><Button aria-label={`编辑连接 ${row.name}`} icon={<Pencil size={16} />} onClick={() => setProviderEditor(row)} /></Tooltip>
      <Tooltip title="删除连接"><Button aria-label={`删除连接 ${row.name}`} danger icon={<Trash2 size={16} />} onClick={() => Modal.confirm({ title: `删除 ${row.name}？`, content: "关联模型须先删除。", okText: "删除", okButtonProps: { danger: true }, cancelText: "取消", onOk: () => perform(`delete-provider-${row.id}`, () => deleteProvider(row.id), "连接已删除") })} /></Tooltip>
    </Space>;
  }

  function modelActions(row: Model) {
    return <Space size="small">
      <Dropdown menu={{ items: capabilities.map((item) => ({ key: item.value, label: `验证${item.label}`, onClick: () => void probe(`model-${row.id}`, async () => (await testModelCapability(row.id, item.value)).result, row.display_name) })) }} trigger={["click"]} disabled={row.status !== 1 || !providerEnabled(row.provider_id)}>
        <Tooltip title="验证能力（可能产生上游费用）"><Button aria-label={`验证能力 ${row.display_name}`} icon={<CheckCircle2 size={16} />} loading={busy === `model-${row.id}`} /></Tooltip>
      </Dropdown>
      <Tooltip title="设为全局默认"><Button aria-label={`设为默认 ${row.display_name}`} icon={<Star size={16} />} disabled={row.is_default || row.status !== 1 || !providerEnabled(row.provider_id) || !row.verified_capabilities.includes("text")} onClick={() => void perform(`default-${row.id}`, () => setDefaultModel(row.id), "默认模型已更新")} /></Tooltip>
      <Dropdown menu={{ items: [
        { key: "test", label: "测试文本调用（可能产生费用）", disabled: row.status !== 1 || !providerEnabled(row.provider_id), onClick: () => void probe(`test-${row.id}`, () => testModel(row.id), row.display_name) },
        { key: "edit", label: "编辑模型", onClick: () => setModelEditor(row) },
        { key: "delete", label: "删除模型", danger: true, onClick: () => Modal.confirm({ title: `删除 ${row.display_name}？`, content: "被 Agent 使用的模型不能删除。", okText: "删除", okButtonProps: { danger: true }, cancelText: "取消", onOk: () => perform(`delete-model-${row.id}`, () => deleteModel(row.id), "模型已删除") }) },
      ] }} trigger={["click"]}><Tooltip title="更多操作"><Button aria-label={`更多操作 ${row.display_name}`} icon={<MoreHorizontal size={16} />} /></Tooltip></Dropdown>
    </Space>;
  }

  const providerColumns: ColumnsType<Provider> = [
    { title: "连接名称", dataIndex: "name", key: "name", render: (value: string, row) => <div className="catalog-primary"><strong>{value}</strong><span>{row.base_url}</span></div> },
    { title: "类型", key: "kind", width: 160, render: (_, row) => kinds.find((kind) => kind.kind === row.kind)?.display_name || row.kind },
    { title: "状态", key: "status", width: 100, render: (_, row) => <Tag color={row.status === 1 ? "success" : "default"}>{row.status === 1 ? "已启用" : "已停用"}</Tag> },
    { title: "凭据", key: "credentials", width: 110, render: (_, row) => row.has_api_key || row.has_custom_headers ? "已配置" : "未配置" },
    { title: "模型", key: "models", width: 80, render: (_, row) => models.filter((model) => model.provider_id === row.id).length },
    { title: "操作", key: "actions", width: 220, render: (_, row) => providerActions(row) },
  ];

  const modelColumns: ColumnsType<Model> = [
    { title: "模型", key: "name", render: (_, row) => <div className="catalog-primary"><strong>{row.display_name}{row.is_default && <Tag color="blue">默认</Tag>}</strong><span>{row.external_id}</span></div> },
    { title: "连接", key: "provider", width: 150, render: (_, row) => providerName(row.provider_id) },
    { title: "能力", key: "capabilities", width: 190, render: (_, row) => <CapabilityTags model={row} /> },
    { title: "窗口 / 价格", key: "pricing", width: 175, render: (_, row) => <div className="catalog-primary"><span>{row.context_window?.toLocaleString() || "未设置"} Token</span><span>{row.input_price && row.output_price ? `${row.price_currency} ${row.input_price} / ${row.output_price}` : "未设置价格"}</span></div> },
    { title: "状态", key: "status", width: 90, render: (_, row) => <Tag color={row.status !== 1 || !providerEnabled(row.provider_id) ? "default" : row.verified_capabilities.includes("text") ? "success" : "warning"}>{row.status !== 1 || !providerEnabled(row.provider_id) ? "停用" : row.verified_capabilities.includes("text") ? "可用于 Agent" : "待验证"}</Tag> },
    { title: "操作", key: "actions", width: 150, render: (_, row) => modelActions(row) },
  ];

  return <div className="workspace catalog-page">
    <PageHeader title="模型与供应商" description="统一管理模型连接，验证能力后供 Agent 与知识库使用。" actions={<Tooltip title="刷新"><Button aria-label="刷新目录" loading={loading} icon={<RefreshCw size={17} />} onClick={() => void reload()} /></Tooltip>} />
    <div className="catalog-surface">
      <Tabs activeKey={tab} onChange={setTab} items={[{ key: "providers", label: `供应商 (${providers.length})` }, { key: "models", label: `模型 (${models.length})` }]} />
      {error && <Alert className="catalog-alert" type="error" showIcon message={error} action={<Button size="small" onClick={() => void reload()}>重试</Button>} />}
      {testResult && <Alert className="catalog-alert" type="info" showIcon message={testResult} closable onClose={() => setTestResult("")} />}
      {tab === "providers" ? <>
        <div className="catalog-toolbar"><Typography.Text type="secondary">共 {providers.length} 个连接</Typography.Text><Button type="primary" icon={<Plus size={17} />} onClick={() => setProviderEditor("new")}>新建连接</Button></div>
        {loading ? <Skeleton active /> : providers.length ? <><div className="catalog-desktop-table"><Table rowKey="id" columns={providerColumns} dataSource={providers} pagination={false} scroll={{ x: "max-content" }} /></div><div className="catalog-mobile-items">{providers.map((row) => <div className="catalog-mobile-item" key={row.id}><div className="catalog-mobile-head"><strong>{row.name}</strong><Tag color={row.status === 1 ? "success" : "default"}>{row.status === 1 ? "已启用" : "已停用"}</Tag></div><div className="catalog-mobile-meta">{kinds.find((item) => item.kind === row.kind)?.display_name || row.kind} · {models.filter((item) => item.provider_id === row.id).length} 个模型</div><div className="catalog-mobile-url">{row.base_url}</div><div className="catalog-mobile-actions">{providerActions(row)}</div></div>)}</div></> : <Empty description="还没有供应商连接" />}
      </> : <>
        <div className="catalog-toolbar"><div className="catalog-filters"><Select aria-label="筛选供应商连接" allowClear placeholder="全部连接" value={providerFilter} onChange={setProviderFilter} options={providers.map((item) => ({ value: item.id, label: item.name }))} /><Input aria-label="搜索模型" prefix={<Search size={16} />} placeholder="搜索模型" value={query} onChange={(event) => setQuery(event.target.value)} allowClear /></div><Button type="primary" icon={<Plus size={17} />} disabled={!providers.some((item) => item.status === 1)} onClick={() => setModelEditor("new")}>录入模型</Button></div>
        <p className="catalog-capability-note">能力标签表示模型配置；带 ✓ 的能力已通过验证，虚线边框表示待验证。</p>
        {loading ? <Skeleton active /> : filteredModels.length ? <><div className="catalog-desktop-table"><Table rowKey="id" columns={modelColumns} dataSource={filteredModels} pagination={{ pageSize: 20, showSizeChanger: false }} scroll={{ x: "max-content" }} /></div><div className="catalog-mobile-items">{filteredModels.map((row) => <div className="catalog-mobile-item" key={row.id}><div className="catalog-mobile-head"><strong>{row.display_name}</strong><Tag color={row.status === 1 && providerEnabled(row.provider_id) && row.verified_capabilities.includes("text") ? "success" : "default"}>{row.status !== 1 || !providerEnabled(row.provider_id) ? "停用" : row.verified_capabilities.includes("text") ? "可用于 Agent" : "待验证"}</Tag></div><div className="catalog-mobile-meta">{row.external_id} · {providerName(row.provider_id)}</div><div className="catalog-mobile-meta">{row.context_window?.toLocaleString() || "未设置"} Token · <CapabilityTags model={row} /></div><div className="catalog-mobile-actions">{modelActions(row)}</div></div>)}</div></> : <Empty description={query || providerFilter ? "没有符合条件的模型" : "还没有模型"} />}
      </>}
    </div>
    <ProviderEditor open={providerEditor !== null} provider={providerEditor === "new" ? null : providerEditor} kinds={kinds} onClose={() => setProviderEditor(null)} onSaved={() => { setProviderEditor(null); void reload(); }} />
    <ModelEditor open={modelEditor !== null} model={modelEditor === "new" ? null : modelEditor} providers={providers} initialProvider={providerFilter} onClose={() => setModelEditor(null)} onSaved={() => { setModelEditor(null); void reload(); }} />
    <Modal title={`从 ${discovery?.provider.name || "供应商"} 导入模型`} open={Boolean(discovery)} onCancel={() => setDiscovery(null)} okText={`导入 (${selected.length})`} okButtonProps={{ disabled: selected.length === 0, loading: busy === "import" }} onOk={() => {
      if (!discovery) return;
      const chosen = discovery.result.models.filter((item) => selected.includes(item.external_id)).map((item) => ({ external_id: item.external_id, display_name: item.display_name, capabilities: ["text"] }));
      setBusy("import");
      void importModels(discovery.provider.id, chosen).then((result) => { message.success(`导入 ${result.created.length} 个模型`); setDiscovery(null); void reload(); }).catch((failure) => setError(errorText(failure))).finally(() => setBusy(""));
    }}>
      {discovery && (discovery.result.available ? discovery.result.models.length ? <div className="catalog-discovery">
        {discovery.result.models.map((item) => <Checkbox key={item.external_id} checked={selected.includes(item.external_id)} onChange={(event) => setSelected((current) => {
          if (!event.target.checked) return current.filter((id) => id !== item.external_id);
          if (current.length >= 100) { message.warning("每次最多导入 100 个模型"); return current; }
          return [...current, item.external_id];
        })} disabled={item.already_added}>{item.display_name}<span>{item.external_id}{item.already_added ? " · 已录入" : ""}</span></Checkbox>)}
      </div> : <Empty description="供应商未返回候选模型，可手动录入" /> : <Alert type="warning" showIcon message={failureLabels[discovery.result.failure || ""] || "模型发现失败，可手动录入"} />)}
    </Modal>
  </div>;
}
