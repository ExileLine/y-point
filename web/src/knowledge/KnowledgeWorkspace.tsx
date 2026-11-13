import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Empty, Form, Grid, InputNumber, List, Modal, Select, Popconfirm, Skeleton, Space, Table, Tabs, Tag, Timeline, Upload, message } from "antd";
import { ArrowLeft, FileText, FolderOpen, RefreshCw, Settings2, UploadCloud } from "lucide-react";
import { ApiError, type User } from "../api";
import {
  cancelKnowledgeJob, deleteKnowledgeBase, deleteKnowledgeDocument, getKnowledgeBase, listKnowledgeDocuments, listKnowledgeJobs,
  rebuildKnowledgeIndex, retryKnowledgeJob, uploadKnowledgeDocument,
  type KnowledgeBase, type KnowledgeDocument, type KnowledgeDocumentOptions, type KnowledgeProcessingMode, type KnowledgeJob, type KnowledgeJobState,
} from "../knowledge-api";
import KnowledgeEditor from "./KnowledgeEditor";
import KnowledgeSearch from "./KnowledgeSearch";
import KnowledgeShares from "./KnowledgeShares";
import KnowledgeDocumentPreview from "./KnowledgeDocumentPreview";

const states: Record<KnowledgeJobState, { label: string; color: string }> = {
  uploaded: { label: "等待处理", color: "default" }, parsing: { label: "解析文档", color: "processing" },
  enriching: { label: "生成知识解释", color: "processing" },
  chunking: { label: "组织证据片段", color: "processing" }, embedding: { label: "生成向量", color: "processing" },
  indexing: { label: "构建索引", color: "processing" }, completed: { label: "已完成", color: "success" },
  failed: { label: "失败", color: "error" }, cancel_requested: { label: "正在取消", color: "warning" }, cancelled: { label: "已取消", color: "default" },
};
const failures: Record<string, string> = {
  parse_failed: "文档解析失败，请检查文件是否损坏或加密。", ocr_required: "此 PDF 没有可用文字；可切换自动提取或 OCR，空白文件需补充有效页面。",
  ocr_unavailable: "本地 OCR 引擎或所需语言包不可用，请由管理员检查 Tesseract、语言包及 OCR 配置。",
  ocr_low_confidence: "OCR 识别质量不足，未发布新索引。请上传更清晰、方向正确的扫描件或补充可选取文字的原件。",
  size_limit: "文档或提取文本超过处理限制，请拆分资料。", cancelled: "任务已取消，可按当前配置重建。",
  superseded: "此索引代已被更新的配置替代，请查看最近任务。", worker_interrupted: "worker 中断，请确认 worker 恢复后重试。",
  timeout: "处理超时，请检查模型与 worker 后重试。", authentication: "模型服务鉴权失败，请由管理员检查凭据。",
  rate_limit: "模型服务触发限流，请稍后重试。", upstream: "模型服务异常，请检查模型服务。",
  invalid_knowledge: "加工模型返回了缺失、越界或截断的结果，旧索引保持不变，请核对模型后重试。",
  invalid_structure: "文档结构无效，请检查表格字段、配置重复键/循环别名/不支持的标签，或 PPTX 内部结构。",
  invalid_embedding: "嵌入响应不符合要求，请重新验证模型。", model_unavailable: "处理模型不可用，请检查模型与供应商配置。",
  budget_exceeded: "超出 Token 或费用预算，请调整预算或文档。", storage_unavailable: "文档存储不可用，请检查私有存储。",
};
export const isActiveKnowledgeJob = (job: KnowledgeJob) => !["completed", "failed", "cancelled"].includes(job.state);
const jobLabel = (state: string) => states[state as KnowledgeJobState]?.label || state;

export default function KnowledgeWorkspace({ id, user, onBack, onUnavailable, onChanged }: {
  id: number; user: User; onBack: () => void; onUnavailable: () => void; onChanged: () => void;
}) {
  const [base, setBase] = useState<KnowledgeBase | null>(null);
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [jobs, setJobs] = useState<KnowledgeJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [operationError, setOperationError] = useState("");
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState("documents");
  const [editing, setEditing] = useState(false);
  const [uploading, setUploading] = useState<KnowledgeDocument | "new" | null>(null);
  const [previewDocument, setPreviewDocument] = useState<KnowledgeDocument | null>(null);
  const [uploadOptions, setUploadOptions] = useState<KnowledgeDocumentOptions>({});
  const [file, setFile] = useState<File | null>(null);
  const [jobDetail, setJobDetail] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState("");
  const generation = useRef(0);
  const mounted = useRef(true);
  const refreshRunning = useRef(false);
  const screens = Grid.useBreakpoint();
  const owned = base?.owner_id === user.id;

  const refresh = useCallback(async (quiet = false) => {
    const seq = ++generation.current;
    refreshRunning.current = true;
    if (!quiet) setLoading(true);
    try {
      const next = await getKnowledgeBase(id);
      const [docs, tasks] = await Promise.all([listKnowledgeDocuments(id), next.owner_id === user.id ? listKnowledgeJobs(id) : Promise.resolve([])]);
      if (!mounted.current || seq !== generation.current) return;
      setBase(next); setDocuments(docs); setJobs(tasks); setError("");
    } catch (failure) {
      if (!mounted.current || seq !== generation.current) return;
      if (failure instanceof ApiError && failure.status === 404) {
        setBase(null); setDocuments([]); setJobs([]); setEditing(false); setUploading(null); setJobDetail(null); onUnavailable();
      } else setError(failure instanceof Error ? failure.message : "知识库加载失败");
    } finally {
      if (seq === generation.current) { refreshRunning.current = false; if (mounted.current) setLoading(false); }
    }
  }, [id, user.id, onUnavailable]);

  useEffect(() => {
    mounted.current = true; void refresh();
    return () => { mounted.current = false; generation.current++; };
  }, [refresh]);
  const activeJobs = jobs.some(isActiveKnowledgeJob);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible" && !refreshRunning.current && !busy && !editing && !uploading) void refresh(true);
    }, activeJobs ? 3000 : 15000);
    const focus = () => { if (!refreshRunning.current && !busy && !editing && !uploading) void refresh(true); };
    window.addEventListener("focus", focus);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", focus); };
  }, [activeJobs, busy, editing, uploading, refresh]);

  async function perform(action: () => Promise<unknown>, success: string) {
    setBusy(true); setError(""); setOperationError("");
    try { await action(); if (mounted.current) { message.success(success); onChanged(); await refresh(true); } }
    catch (failure) {
      if (!mounted.current) return;
      const text = failure instanceof Error ? failure.message : "操作失败";
      await refresh(true);
      if (mounted.current) setOperationError(`${text}${failure instanceof ApiError && failure.status === 409 ? "。已尝试刷新当前版本，请核对后重新操作；不会自动重试。" : ""}`);
    } finally { if (mounted.current) setBusy(false); }
  }

  async function upload() {
    if (!base || !file || !uploading) return;
    setBusy(true); setUploadError("");
    try {
      const response = await uploadKnowledgeDocument(base.id, file, base.revision, uploading === "new" ? undefined : uploading.id, uploadOptions);
      if (!mounted.current) return;
      message.success(response.deduplicated ? "文档内容相同，已去重，无需重复索引" : `文档已保存，索引任务 #${response.job_id} 已排队`);
      setUploading(null); setFile(null); if (response.job_id) { setTab("jobs"); setJobDetail(response.job_id); }
      onChanged(); await refresh(true);
    } catch (failure) {
      if (!mounted.current) return;
      await refresh(true);
      if (mounted.current) setUploadError(`${failure instanceof Error ? failure.message : "上传失败"}${failure instanceof ApiError && failure.status === 409 ? "。已刷新版本，请核对同名文档、知识库版本和嵌入配置后重试。" : ""}`);
    } finally { if (mounted.current) setBusy(false); }
  }

  if (loading && !base) return <div className="kb-page kb-surface"><Skeleton active paragraph={{ rows: 8 }} /></div>;
  if (!base) return <div className="kb-page kb-stack"><Button onClick={onBack} icon={<ArrowLeft size={16} />}>返回知识库</Button><Alert type="error" showIcon message={error || "知识库暂不可用"} action={<Button onClick={() => void refresh()}>重试</Button>} /></div>;
  const latest = jobs[0];
  const selectedJob = jobs.find((job) => job.id === jobDetail);
  const pending = latest && isActiveKnowledgeJob(latest);
  const openUpload = (item: KnowledgeDocument | "new") => { setUploadOptions(item === "new" ? {} : item.processing_options || {}); setFile(null); setUploadError(""); setUploading(item); };

  const documentActions = (item: KnowledgeDocument) => <Space size="small" wrap>
    <Button type="link" size="small" onClick={() => setPreviewDocument(item)}>加工结果</Button>
    {owned ? <>
    <Button type="link" size="small" disabled={busy} onClick={() => openUpload(item)}>更新版本</Button>
    <Popconfirm title={`删除「${item.filename}」？`} description="成功删除后，所有索引代中的该文档与旧引用都会不可访问。" okText="删除" cancelText="保留" disabled={busy}
      onConfirm={() => perform(() => deleteKnowledgeDocument(id, item.id, base.revision), "文档已删除，新索引已排队")}>
      <Button type="text" danger size="small" disabled={busy} aria-label={`删除文档 ${item.filename}`}>删除</Button>
    </Popconfirm></> : <Tag>只读</Tag>}</Space>;

  return <div className="kb-page kb-stack">
    <Button className="kb-back" type="text" icon={<ArrowLeft size={16} />} onClick={onBack}>全部知识库</Button>
    <div className="kb-page-heading"><div className="kb-title-block"><div className="kb-inline"><span className="kb-resource-icon"><FolderOpen size={24} /></span><div><h1>{base.name}</h1><span className="kb-muted">{owned ? "我的知识库" : `由用户 #${base.owner_id} 共享`} · 配置版本 r{base.revision}</span></div></div><p className="kb-description">{base.description || "集中管理文档，为检索和 Agent 回答提供可追溯的证据。"}</p></div>
      <Space wrap><Button icon={<RefreshCw size={16} />} aria-label="刷新知识库" loading={loading} disabled={busy} onClick={() => void refresh()}>刷新</Button>{owned && <Button icon={<Settings2 size={16} />} disabled={busy} onClick={() => setEditing(true)}>设置</Button>}</Space>
    </div>
    {(error || operationError) && <Alert type="error" showIcon closable onClose={() => { setError(""); setOperationError(""); }} message={error || operationError} action={<Button size="small" onClick={() => void refresh()}>刷新</Button>} />}
    <div className="kb-summary-grid">
      <div className="kb-summary"><span>文档</span><strong>{documents.length}<small> / 100</small></strong><span>目录为当前上传版本</span></div>
      <div className="kb-summary"><span>已发布索引</span><strong>{base.active_index_id ? `#${base.active_index_id}` : "尚未发布"}</strong><span>{pending ? base.active_index_id ? "新代构建中，原索引继续服务" : "首个索引构建中，尚不可检索" : base.active_index_id ? "检索使用此固定索引代" : "索引发布后可用于检索"}</span></div>
      <div className="kb-summary"><span>当前配置策略</span><strong>{base.search_mode === "hybrid" ? "混合检索" : "关键词检索"}</strong><span>{base.search_mode === "hybrid" ? `嵌入模型 #${base.embedding_model_id}` : "无需调用嵌入模型"} · 新代发布后生效</span></div>
    </div>
    {owned && latest && <Alert type={latest.state === "failed" ? "error" : pending ? "info" : latest.state === "completed" ? "success" : "warning"} showIcon
      message={`最近任务 #${latest.id} · ${jobLabel(latest.state)}`}
      description={latest.state === "uploaded" ? "任务等待独立 worker 领取；长时间无变化时请检查 worker 是否启动。" : latest.state === "failed" ? `${latest.failure_code || "处理失败"}；${base.active_index_id ? "已有完整索引保持可用。" : "尚无可用索引，请处理失败原因后重试。"}` : latest.state === "completed" && latest.index_id !== base.active_index_id ? "此任务已完成，当前服务索引以「已发布索引」为准。" : "阶段与状态由服务端任务记录提供。"}
      action={<Button size="small" onClick={() => { setTab("jobs"); setJobDetail(latest.id); }}>查看任务</Button>} />}
    <div className="kb-surface"><Tabs activeKey={tab} onChange={setTab} destroyInactiveTabPane items={[
      { key: "documents", label: `文档 · ${documents.length}`, children: <div className="kb-stack">
        <div className="kb-section-heading"><div><h2 className="kb-section-title">文档资料</h2><p className="kb-muted">新版本完整索引成功后才替换旧代。目录版本可能领先于检索中的版本。</p></div>{owned && <Button type="primary" icon={<UploadCloud size={16} />} disabled={busy || documents.length >= 100} onClick={() => openUpload("new")}>上传文档</Button>}</div>
        {screens.md ? <Table rowKey="id" dataSource={documents} pagination={false} locale={{ emptyText: <Empty description={owned ? "还没有文档，上传第一份资料开始构建" : "此知识库暂时没有文档"} /> }} columns={[
          { title: "文件名", dataIndex: "filename", render: (name: string) => <span className="kb-document-name"><FileText size={17} /><span>{name}</span></span> },
          { title: "上传版本", dataIndex: "current_version", render: (version: number) => <Tag>v{version}</Tag> },
          { title: "更新时间", dataIndex: "update_time", render: (value: string) => <span className="kb-muted">{value}</span> },
          { title: "操作", render: (_: unknown, item: KnowledgeDocument) => documentActions(item) },
        ]} /> : <List dataSource={documents} locale={{ emptyText: <Empty description="暂无文档" /> }} renderItem={(item) => <List.Item className="kb-mobile-document"><div className="kb-document-name"><FileText size={17} /><strong>{item.filename}</strong></div><div className="kb-muted">v{item.current_version} · {item.update_time}</div>{documentActions(item)}</List.Item>} />}
        <Alert type="info" showIcon message="支持 TXT、Markdown、PDF、DOCX、XLSX、CSV、HTML、JSON、YAML、PPTX，单文件不超过 10 MiB" description="表格保留完整记录；PDF 支持文字层与本地 OCR；HTML 提取主文，JSON/YAML 保留配置路径，PPTX 区分正文、表格与备注。每次上传一个文件；相同文件及有效处理选项会去重，修改内容或提取方式请使用「更新版本」。" />
      </div> },
      ...(owned ? [{ key: "jobs", label: "索引任务", children: <div className="kb-stack">
        <div className="kb-section-heading"><div><h2 className="kb-section-title">索引任务</h2><p className="kb-muted">显示最近 50 项。处理中每 3 秒刷新；完成并不保证此代仍是当前活动代。</p></div><Popconfirm title="按当前配置重建完整索引？" description="构建期间保留原索引；最新期望代完整成功后才会发布。" okText="重建" cancelText="取消" onConfirm={() => perform(() => rebuildKnowledgeIndex(id, base.revision), "重建任务已排队")}><Button icon={<RefreshCw size={16} />} disabled={busy}>重建索引</Button></Popconfirm></div>
        <List dataSource={jobs} locale={{ emptyText: <Empty description="暂无索引任务，上传文档后会自动创建" /> }} renderItem={(job) => <List.Item className="kb-job-row">
          <div className="kb-stack-tight"><div className="kb-inline"><strong>任务 #{job.id}</strong><Tag color={states[job.state]?.color}>{jobLabel(job.state)}</Tag>{job.index_id === base.active_index_id && <Tag color="blue">当前已发布</Tag>}</div><span className="kb-muted">索引 #{job.index_id} · 尝试 {job.attempts}/3 · {job.update_time}</span>{job.failure_code && <span className="kb-error-text">{job.failure_code}</span>}</div>
          <Space wrap><Button size="small" onClick={() => setJobDetail(job.id)}>详情</Button>{["failed", "cancelled"].includes(job.state) && job.attempts > 0 && job.attempts < 3 && <Button size="small" disabled={busy} onClick={() => void perform(() => retryKnowledgeJob(job.id, job.attempts), "任务已重新排队")}>重试</Button>}{isActiveKnowledgeJob(job) && job.state !== "cancel_requested" && <Popconfirm title="取消这个索引任务？" okText="请求取消" cancelText="继续处理" onConfirm={() => perform(() => cancelKnowledgeJob(job.id), "取消请求已提交，请等待任务终态")}><Button size="small" danger disabled={busy}>取消任务</Button></Popconfirm>}</Space>
        </List.Item>} />
      </div> }] : []),
      { key: "search", label: "检索测试", children: <KnowledgeSearch base={base} onAccessCheck={() => refresh(true)} /> },
      ...(owned ? [{ key: "shares", label: "共享设置", children: <KnowledgeShares baseId={id} onAccessCheck={() => refresh(true)} /> }] : []),
      ...(owned ? [{ key: "manage", label: "管理", children: <div className="kb-stack"><h2 className="kb-section-title">删除知识库</h2><p className="kb-muted">成功后立即阻断此知识库的检索与旧引用，并请求取消相关任务和运行。原文件在后台清理。</p><Popconfirm title={`删除知识库「${base.name}」？`} description="请确认相关 Agent 不再需要该资料，此操作不能在界面恢复。" okText="删除知识库" cancelText="保留" onConfirm={async () => {
        setBusy(true);
        try { await deleteKnowledgeBase(id, base.revision); message.success("知识库已删除"); onUnavailable(); }
        catch (failure) { await refresh(true); if (mounted.current) setOperationError(failure instanceof Error ? failure.message : "删除失败"); }
        finally { if (mounted.current) setBusy(false); }
      }}><Button danger disabled={busy}>删除知识库</Button></Popconfirm></div> }] : []),
    ]} /></div>
    {previewDocument && <KnowledgeDocumentPreview key={`${id}:${base.active_index_id}:${previewDocument.id}`} baseId={id} document={previewDocument} onClose={() => setPreviewDocument(null)} />}
    {editing && <KnowledgeEditor item={base} user={user} onClose={() => setEditing(false)} onSaved={() => { setEditing(false); onChanged(); void refresh(true); }} />}
    <Modal open={uploading !== null} title={uploading === "new" ? "上传文档" : `更新文档版本 · ${uploading?.filename || ""}`} okText="上传并建立索引" cancelText="取消" confirmLoading={busy} onOk={() => void upload()} okButtonProps={{ disabled: !file }} onCancel={busy ? undefined : () => setUploading(null)} closable={!busy} maskClosable={!busy} keyboard={!busy}>
      <div className="kb-stack">{uploadError && <Alert type="error" showIcon message={uploadError} />}
        <p className="kb-muted">文件将保存在私有存储中。上传成功后由独立 worker 解析、分块和构建索引。</p>
        <Upload.Dragger accept=".txt,.md,.pdf,.docx,.xlsx,.csv,.html,.htm,.json,.yaml,.yml,.pptx" multiple={false} maxCount={1} disabled={busy} fileList={file ? [{ uid: "selected", name: file.name }] : []} onRemove={() => { setFile(null); return true; }} beforeUpload={(selected) => {
          if (!/\.(txt|md|pdf|docx|xlsx|csv|html|htm|json|yaml|yml|pptx)$/i.test(selected.name)) { message.error("支持 TXT、Markdown、PDF、DOCX、XLSX、CSV、HTML、JSON、YAML、PPTX"); return Upload.LIST_IGNORE; }
          if (selected.size === 0 || selected.size > 10 * 1024 * 1024) { message.error("文件须非空且不超过 10 MiB"); return Upload.LIST_IGNORE; }
          if (!/\.xlsx$/i.test(selected.name)) setUploadOptions(current => ({ ...current, sheet_names: [], ...(!/\.csv$/i.test(selected.name) ? { header_rows: 1 } : {}) }));
          if (!/\.pdf$/i.test(selected.name)) setUploadOptions(current => ({ ...current, pdf_mode: "auto" }));
          setFile(selected); setUploadError(""); return false;
        }}><UploadCloud className="kb-upload-icon" size={32} /><p>点击选择或拖入文档</p><span className="kb-muted">每次 1 个文件，最大 10 MiB</span></Upload.Dragger>
        <Form layout="vertical" requiredMark={false}>
          <Form.Item label="本文件处理方式" extra="可覆盖知识库默认策略；增强理解需要知识库已配置加工模型。">
            <Select<KnowledgeProcessingMode | "inherit"> aria-label="本文件处理方式" value={uploadOptions.processing_mode || "inherit"} disabled={busy} onChange={value => setUploadOptions(current => ({ ...current, processing_mode: value === "inherit" ? null : value }))}
              options={[{ value: "inherit", label: "跟随知识库设置" }, { value: "auto", label: "自动处理" }, { value: "original", label: "保留原文" }, { value: "enhanced", label: "增强理解" }]} />
          </Form.Item>
          {file && /\.(html?|json|ya?ml|pptx)$/i.test(file.name) && <Alert type="info" showIcon message="按格式保留可追溯结构"
            description={/\.html?$/i.test(file.name) ? "HTML 仅提取主文、标题和表格，不执行脚本或加载外链资源。" : /\.pptx$/i.test(file.name) ? "PPTX 保留页码、表格与演讲者备注，排除隐藏页和隐藏对象；图片、图表不作语义识别。" : "配置按路径、字段类型和值建立证据，不执行标签或变量替换；重复键、循环别名及 YAML 合并键会拒绝发布。"} />}
          {file && /\.pdf$/i.test(file.name) && <>
            <Form.Item label="PDF 页面提取" extra="自动按页判断文字层与扫描内容；OCR 在服务器本地运行。仅提取文字层不会识别扫描页，强制 OCR 可用于文字层乱码。">
              <Select aria-label="PDF 页面提取" value={uploadOptions.pdf_mode || "auto"} disabled={busy}
                onChange={value => setUploadOptions(current => ({ ...current, pdf_mode: value }))}
                options={[{ value: "auto", label: "自动 · 文字层优先，扫描页 OCR" }, { value: "text", label: "仅提取文字层" }, { value: "ocr", label: "强制 OCR · 逐页识别" }]} />
            </Form.Item>
            <Alert type="info" showIcon message="保留页码、来源区域和 OCR 质量" description="空白页会单独记录；识别质量不足时保留旧索引。扫描表格先保留识别行，表格关系和数值须核对原页。" />
          </>}
          {file && /\.(xlsx|csv)$/i.test(file.name) && <>
            <Alert className="kb-form-note" type="info" showIcon message="按表头组织完整记录，原始数值独立保留" description="每个区域首行默认作为表头，合并多级表头可调整行数。隐藏工作表、行、列不进入知识；公式不执行，仅展示表达式和已有缓存结果。" />
            <Form.Item label="表头行数" extra="适用于每个表格区域；标题或说明行需先整理，避免误当字段。"><InputNumber aria-label="表头行数" min={1} max={5} precision={0} value={uploadOptions.header_rows || 1} onChange={value => setUploadOptions(current => ({ ...current, header_rows: value || 1 }))} disabled={busy} /></Form.Item>
            {/\.xlsx$/i.test(file.name) && <Form.Item label="仅处理指定工作表" extra="留空处理全部可见工作表；输入名称后按回车添加，名称必须与文件一致。"><Select aria-label="仅处理指定工作表" mode="tags" value={uploadOptions.sheet_names || []} onChange={sheet_names => setUploadOptions(current => ({ ...current, sheet_names }))} disabled={busy} /></Form.Item>}
          </>}
        </Form>
        {file && <span className="kb-muted">已选择 {file.name}，点击「上传并建立索引」后才会发送。</span>}
      </div>
    </Modal>
    <Drawer open={jobDetail !== null} title={`索引任务 #${jobDetail || ""}`} width="var(--yp-detail-width)" onClose={() => setJobDetail(null)}>
      {selectedJob ? <div className="kb-stack"><Tag color={states[selectedJob.state]?.color}>{jobLabel(selectedJob.state)}</Tag>
        {selectedJob.failure_code && <Alert type="error" showIcon message={selectedJob.failure_code} description={failures[selectedJob.failure_code] || "请核对模型、预算和 worker 状态。任务已过期或重试次数用尽时，请重建索引。"} />}
        <Descriptions column={1} size="small" items={[
          { key: "index", label: "索引代", children: `#${selectedJob.index_id}${selectedJob.index_id === base.active_index_id ? " · 当前已发布" : ""}` },
          { key: "attempt", label: "尝试次数", children: `${selectedJob.attempts} / 3` },
          { key: "processing-calls", label: "加工模型调用", children: selectedJob.processing_call_count ?? 0 },
          { key: "processing-tokens", label: "加工已报告 Token", children: selectedJob.processing_tokens == null ? "未报告" : selectedJob.processing_tokens.toLocaleString() },
          { key: "processing-reserved", label: "加工预留 Token", children: (selectedJob.processing_reserved_tokens ?? 0).toLocaleString() },
          { key: "processing-cost", label: "加工估算费用", children: selectedJob.processing_estimated_cost ? `${selectedJob.processing_estimated_cost} ${selectedJob.processing_usage?.[0]?.currency || ""}` : "未报告" },
          { key: "tokens", label: "嵌入 Token", children: selectedJob.embedding_tokens.toLocaleString() },
          { key: "reserved", label: "预留 Token", children: selectedJob.reserved_tokens.toLocaleString() },
          { key: "cost", label: "估算成本", children: selectedJob.estimated_cost ?? "未记录" },
        ]} /><Timeline items={selectedJob.progress.map((progress, index) => ({ key: index, color: progress.stage === "failed" ? "red" : "blue", children: <div><strong>{jobLabel(progress.stage)}{progress.skipped ? " · 已跳过" : ""}</strong><div className="kb-muted">{progress.time}</div></div> }))} />
        {selectedJob.state === "uploaded" && <Alert type="info" showIcon message="等待 worker 领取" description="处理没有固定完成时间；若阶段长期不变，请检查独立 worker 的启动情况与日志。" />}
      </div> : <Empty description="任务不在最近列表中，请刷新或查看其他任务" />}
    </Drawer>
  </div>;
}
