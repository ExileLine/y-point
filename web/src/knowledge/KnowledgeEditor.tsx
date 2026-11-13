import { useEffect, useState } from "react";
import { Alert, Button, Collapse, Drawer, Form, Input, InputNumber, Select, Space, message } from "antd";
import { ApiError, listModels, type Model, type User } from "../api";
import { createKnowledgeBase, testEmbeddingModel, updateKnowledgeBase, type KnowledgeBase, type KnowledgeDraft } from "../knowledge-api";

const defaults: KnowledgeDraft = {
  name: "", description: null, search_mode: "hybrid", embedding_model_id: null,
  chunk_size: 800, chunk_overlap: 100, embedding_token_budget: null, embedding_cost_budget: null,
  processing_mode: "auto", processing_model_id: null, processing_token_budget: null, processing_cost_budget: null,
};

export default function KnowledgeEditor({ item, user, onClose, onSaved }: {
  item: KnowledgeBase | null; user: User; onClose: () => void; onSaved: (id: number) => void;
}) {
  const [original] = useState(item);
  const [form] = Form.useForm<KnowledgeDraft>();
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [models, setModels] = useState<Model[]>([]);
  const [modelError, setModelError] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const mode = Form.useWatch("search_mode", form);
  const processingMode = Form.useWatch("processing_mode", form);
  const modelId = Form.useWatch("embedding_model_id", form);

  useEffect(() => {
    if (user.role !== "admin") return;
    let active = true;
    listModels().then((rows) => { if (active) setModels(rows); })
      .catch((failure: unknown) => { if (active) setModelError(failure instanceof Error ? failure.message : "模型目录加载失败"); });
    return () => { active = false; };
  }, [user.role]);

  async function save(values: KnowledgeDraft) {
    setBusy(true); setError("");
    const draft: KnowledgeDraft = {
      ...values, name: values.name.trim(), description: values.description?.trim() || null,
      embedding_model_id: values.search_mode === "hybrid" ? values.embedding_model_id : null,
      embedding_token_budget: values.embedding_token_budget ?? null,
      embedding_cost_budget: values.embedding_cost_budget || null,
      processing_model_id: values.processing_model_id || null,
      processing_token_budget: values.processing_token_budget ?? null,
      processing_cost_budget: values.processing_cost_budget || null,
    };
    try {
      const id = item ? (await updateKnowledgeBase(item.id, draft, original!.revision)).knowledge_base.id : (await createKnowledgeBase(draft)).id;
      message.success(item ? "配置已保存，新索引已排队；发布前继续使用原索引" : "知识库已创建，可以上传文档了");
      onSaved(id);
    } catch (failure) {
      setConflict(failure instanceof ApiError && failure.status === 409);
      setError(failure instanceof Error ? failure.message : "保存失败");
    } finally { setBusy(false); }
  }

  async function probe() {
    if (!modelId) return;
    setTesting(true); setModelError("");
    try {
      const result = await testEmbeddingModel(modelId);
      if (!result.success) { setModelError(`嵌入验证失败：${result.failure_code || "未知错误"}`); return; }
      message.success(`嵌入验证通过：${result.dimensions} 维，${Math.round(result.latency_ms)} ms`);
      setModels(await listModels());
    } catch (failure) { setModelError(failure instanceof Error ? failure.message : "嵌入验证失败"); }
    finally { setTesting(false); }
  }

  const options = models.filter((model) => model.status === 1 && (model.capabilities.includes("embedding") || model.verified_capabilities.includes("embedding")))
    .map((model) => ({ value: model.id, label: `${model.display_name} · ${model.verified_capabilities.includes("embedding") ? "已验证" : "待验证"}` }));
  if (modelId && !options.some((option) => option.value === modelId)) options.unshift({ value: modelId, label: `模型 #${modelId}（请确认可用性）` });

  return <Drawer open title={item ? "知识库设置" : "新建知识库"} width="var(--yp-detail-width)" onClose={busy || testing ? undefined : onClose}
    maskClosable={!busy && !testing} keyboard={!busy && !testing}
    extra={<Button type="primary" loading={busy} disabled={testing || conflict} onClick={() => form.submit()}>{item ? "保存并重建" : "创建"}</Button>}>
    <div className="kb-stack">
      {error && <Alert type="error" showIcon message={error} description={conflict ? "请关闭设置并刷新知识库，核对最新版本或模型配置后再编辑。系统不会自动覆盖或重试。" : undefined} />}
      <Alert type="info" showIcon message={item ? "完整新索引发布后，检索才会使用本次配置" : "默认仅自己可见，可在创建后授予指定用户只读权限"} />
      <Form form={form} layout="vertical" initialValues={item ? { ...defaults, ...item } : defaults} onFinish={save} requiredMark={false} disabled={busy || testing}>
        <Form.Item name="name" label="名称" rules={[{ required: true, whitespace: true, message: "请输入知识库名称" }]}><Input maxLength={128} showCount /></Form.Item>
        <Form.Item name="description" label="用途说明"><Input.TextArea rows={3} maxLength={1024} showCount placeholder="例如：人事制度、差旅报销规则，供员工助手引用" /></Form.Item>
        <Form.Item name="processing_mode" label="文档处理方式" rules={[{ required: true }]} extra="按格式保留表格记录、PDF 区域、主文标题、配置路径或幻灯片来源，模型解释单独保存。新配置在完整索引发布后生效。">
          <Select options={[{ value: "auto", label: "自动处理 · 按格式选择" }, { value: "original", label: "保留原文 · 不调用加工模型" }, { value: "enhanced", label: "增强理解 · 文档语义解释" }]} />
        </Form.Item>
        <Alert className="kb-form-note" type="info" showIcon message={processingMode === "original" ? "按结构保留证据，不产生加工模型费用" : "模型解释与原始结构证据分别保存"} description="自动模式配置模型后加工表格及含 OCR／已识别表格的 PDF；HTML、JSON/YAML、PPTX 和纯文字 PDF 仅在增强模式下加工。PDF 本地 OCR 由页面提取选项决定，不消耗模型 Token。" />
        <Form.Item name="processing_model_id" label="文档加工模型" rules={[{ required: processingMode === "enhanced", message: "增强理解需要选择加工模型" }]} extra="独立于嵌入模型和 Agent 对话模型。保留原文模式下也可预先配置，供文档覆盖策略使用。">
          {user.role === "admin" ? <Select allowClear showSearch optionFilterProp="label" options={models.filter(model => model.status === 1 && model.verified_capabilities.includes("text")).map(model => ({ value: model.id, label: model.display_name }))} placeholder="选择已验证文本能力的模型" /> : <InputNumber min={1} precision={0} className="kb-full-width" placeholder="填写管理员提供的文本模型 ID" />}
        </Form.Item>
        {modelError && mode !== "hybrid" && <Alert type="warning" showIcon message={modelError} />}
        <Form.Item name="search_mode" label="检索策略" rules={[{ required: true }]} extra="关键词检索使用全文索引；混合检索融合关键词与向量候选，不随机切换。">
          <Select options={[{ value: "keyword", label: "关键词检索" }, { value: "hybrid", label: "混合检索 · 关键词 + 向量" }]} />
        </Form.Item>
        {mode === "hybrid" && <>
          <Form.Item name="embedding_model_id" label="嵌入模型" rules={[{ required: true, message: "请选择或填写嵌入模型 ID" }]} extra="模型须通过独立嵌入验证；文本生成能力验证不能替代此验证。">
            {user.role === "admin" ? <Select showSearch optionFilterProp="label" options={options} placeholder="选择已配置的嵌入模型" /> : <InputNumber min={1} precision={0} className="kb-full-width" placeholder="填写管理员提供的嵌入模型 ID" />}
          </Form.Item>
          {user.role === "admin" && <Space className="kb-form-note" wrap><Button loading={testing} disabled={!modelId || busy} onClick={() => void probe()}>验证嵌入能力</Button><span className="kb-muted">会调用该模型的嵌入接口</span></Space>}
          {modelError && <Alert className="kb-form-note" type="warning" showIcon message={modelError} />}
        </>}
        <Collapse ghost items={[{ key: "advanced", label: "分块与索引预算", forceRender: true, children: <>
          <Form.Item name="processing_token_budget" label="单次加工 Token 预留上限" rules={[{ type: "number", min: 1, max: 2000000 }]} extra="留空不限制累计 Token。填写时按输入与输出保守预留，失败和重试仍累计；模型单次请求限制与费用预算仍生效。"><InputNumber min={1} max={2000000} precision={0} placeholder="留空不限制累计 Token" className="kb-full-width" /></Form.Item>
          <Form.Item name="processing_cost_budget" label="单次加工估算费用上限" extra="按加工模型币种计算，须配置输入和输出价格；与嵌入费用分别统计。"><InputNumber min="0.000001" max="999999999999.999999" precision={6} stringMode className="kb-full-width" /></Form.Item>
          <div className="kb-form-pair">
            <Form.Item name="chunk_size" label="片段大小（字符）" rules={[{ required: true, type: "number", min: 200, max: 2000 }]}><InputNumber min={200} max={2000} precision={0} className="kb-full-width" /></Form.Item>
            <Form.Item name="chunk_overlap" label="重叠字符" dependencies={["chunk_size"]} rules={[{ required: true, type: "number", min: 0, max: 500 }, { validator: async (_, value) => {
              if (value >= Math.floor(form.getFieldValue("chunk_size") / 2)) throw new Error("重叠须小于片段大小的一半");
            } }]}><InputNumber min={0} max={500} precision={0} className="kb-full-width" /></Form.Item>
          </div>
          <Form.Item name="embedding_token_budget" label="单次索引嵌入 Token 预算" rules={[{ type: "number", min: 1, max: 2000000 }]} extra="留空不限制累计 Token；模型单批输入限制与独立费用预算仍生效。"><InputNumber min={1} max={2000000} precision={0} placeholder="留空不限制累计 Token" className="kb-full-width" /></Form.Item>
          <Form.Item name="embedding_cost_budget" label="单次索引估算成本上限" extra="按模型配置币种计算；留空不设置金额上限。模型须配置价格。"><InputNumber min="0.000001" max="999999999999.999999" precision={6} stringMode className="kb-full-width" /></Form.Item>
        </> }]} />
      </Form>
    </div>
  </Drawer>;
}
