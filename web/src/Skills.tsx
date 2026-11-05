import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Empty, Input, Modal, Popconfirm, Select, Space, Switch, Tag, Tooltip, Typography, Upload, message } from "antd";
import { Download, FileArchive, FilePlus2, Pencil, Plus, RefreshCw, Search, Trash2, Upload as UploadIcon } from "lucide-react";
import { downloadArtifact, publishSkillTemplates, createSkill, deleteSkill, downloadSkill, getSkill, importSkill, listSkills, updateSkill, type Skill } from "./api";

import { PageHeader, ResourceCard, ResourceCollection } from "./components/ResourcePage";

const defaultFieldsSchema = JSON.stringify({ type: "object", properties: { title: { type: "string" }, summary: { type: "string" } }, required: ["title", "summary"] }, null, 2);
type TemplateDraft = { id: string; file: File; schema: string };
const templateKey = (name: string) => name.normalize("NFC").toLowerCase();
const templatesOf = (skill: Skill) => skill.report_templates || (skill.report_template ? [skill.report_template] : []);

const starter = "---\nname: my-skill\ndescription: 简要说明何时使用这个 Skill\n---\n\n# 操作说明\n\n";

export default function Skills({ userId }: { userId: number }) {
  const [items, setItems] = useState<Skill[]>([]);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("all");
  const [group, setGroup] = useState("all");
  const [editorError, setEditorError] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<Skill | null | "new">(null);
  const [content, setContent] = useState(starter);
  const [templateDrafts, setTemplateDrafts] = useState<TemplateDraft[]>([]);
  const [category, setCategory] = useState("");
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadVisibility, setUploadVisibility] = useState<"keep" | "private" | "public">("keep");

  async function reload() {
    setLoading(true);
    try { setItems(await listSkills()); setError(""); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Skill 列表加载失败"); }
    finally { setLoading(false); }
  }

  useEffect(() => { void reload(); }, []);

  async function openEditor(item: Skill | "new") {
    setEditorError(""); setTemplateDrafts([]);
    if (item === "new") {
      setEditing("new"); setContent(starter); setCategory(""); setVisibility("private"); setEnabled(true);
      return;
    }
    try {
      const full = await getSkill(item.id);
      setEditing(full); setContent(full.files?.["SKILL.md"] || "");
      setCategory(full.category || ""); setVisibility(full.visibility); setEnabled(full.enabled);
    } catch (failure) { message.error(failure instanceof Error ? failure.message : "Skill 加载失败"); }
  }

  async function save() {
    if (!editing) return;
    setBusy(true); setEditorError("");
    try {
      if (editing === "new") {
        await createSkill({ files: { "SKILL.md": content }, category: category || null, visibility });
      } else {
        const saved = await updateSkill(editing.id, { expected_version: editing.current_version,
          files: { ...editing.files, "SKILL.md": content }, category: category || null, visibility, enabled });
        if (templateDrafts.length) { setEditing(saved); message.success("Skill 正文已保存，可继续发布所选模板"); await reload(); return; }
      }
      message.success("Skill 已保存"); setEditing(null); await reload();
    } catch (failure) { setEditorError(failure instanceof Error ? failure.message : "保存失败"); }
    finally { setBusy(false); }
  }

  async function publishTemplate() {
    if (!editing || editing === "new" || !templateDrafts.length) return;
    setBusy(true); setEditorError("");
    try {
      const templates = templateDrafts.map(item => {
        try { return { file: item.file, schema: JSON.parse(item.schema) as Record<string, unknown> }; }
        catch { throw new Error(`${item.file.name} 的报告字段 JSON 格式不正确`); }
      });
      const updated = await publishSkillTemplates(editing.id, editing.current_version, templates);
      setEditing(updated); setTemplateDrafts([]);
      message.success(`${templates.length} 个模板已发布为 v${updated.current_version}，请在 Agent 中重新关联此 Skill 以使用新版`);
      await reload();
    } catch (failure) { setEditorError(failure instanceof Error ? failure.message : "模板发布失败"); }
    finally { setBusy(false); }
  }

  function addTemplates(incoming: File[]) {
    const existing = editing && editing !== "new" ? templatesOf(editing) : [];
    const next = [...templateDrafts];
    for (const file of incoming) {
      const key = templateKey(file.name);
      if (!key.endsWith(".docx") || file.size > 2 * 1024 * 1024) { message.error(`${file.name}：请选择不超过 2 MiB 的 DOCX 模板`); continue; }
      if (next.some(item => templateKey(item.file.name) === key)) { message.warning(`${file.name} 已在待发布列表中`); continue; }
      if (new Set([...existing.map(item => templateKey(item.filename)), ...next.map(item => templateKey(item.file.name)), key]).size > 10) {
        message.error("每个 Skill 最多 10 个模板"); continue;
      }
      const previous = existing.find(item => templateKey(item.filename) === key);
      next.push({ id: crypto.randomUUID(), file, schema: previous ? JSON.stringify(previous.fields_schema, null, 2) : defaultFieldsSchema });
    }
    setTemplateDrafts(next);
  }
  const unsaved = editing && editing !== "new" && (content !== editing.files?.["SKILL.md"] || category !== (editing.category || "") || visibility !== editing.visibility || enabled !== editing.enabled);

  async function doImport(file: File) {
    setBusy(true);
    try {
      const result = await importSkill(file, uploadVisibility === "keep" ? undefined : uploadVisibility);
      message.success(`已导入 ${result.name}，共 ${result.manifest?.length || 0} 个文件`);
      setUploadOpen(false); await reload();
    } catch (failure) { message.error(failure instanceof Error ? failure.message : "导入失败"); }
    finally { setBusy(false); }
  }

  const filtered = items.filter((item) => `${item.name} ${item.description} ${item.category || ""}`.toLowerCase().includes(query.trim().toLowerCase())
    && (scope === "all" || (scope === "mine" ? item.owner_id === userId : item.owner_id !== userId))
    && (group === "all" || item.category === group));
  const groups = [...new Set(items.map((item) => item.category).filter((value): value is string => Boolean(value)))];
  async function download(id?: number) {
    try { await downloadSkill(id); } catch (failure) { message.error(failure instanceof Error ? failure.message : "下载失败"); }
  }
  const tags = (item: Skill) => <><Tag>{item.visibility === "public" ? "公开" : "私有"}</Tag><Tag color={item.enabled ? "success" : "default"}>{item.enabled ? "已启用" : "已停用"}</Tag></>;
  const actions = (item: Skill) => <Space size="small" wrap>
    <Button type="text" icon={<Download size={16} />} aria-label="导出" onClick={() => void download(item.id)}>导出</Button>
    {item.owner_id === userId && <><Button type="text" icon={<Pencil size={16} />} aria-label="编辑" onClick={() => void openEditor(item)}>编辑</Button>
      <Popconfirm title="删除 Skill？" description="删除后将无法继续使用此 Skill。" onConfirm={async () => { try { await deleteSkill(item.id); await reload(); } catch (failure) { message.error(failure instanceof Error ? failure.message : "删除失败"); } }}>
        <Button type="text" danger icon={<Trash2 size={16} />} aria-label="删除">删除</Button>
      </Popconfirm></>}
  </Space>;
  return <div className="skills-page resource-page">
    <PageHeader title="Skill" description="把可复用的指令与资料整理为技能包，供 Agent 按需使用。" actions={<>
      <Button icon={<Download size={16} />} onClick={() => void download()}>下载模板</Button>
      <Button icon={<UploadIcon size={16} />} onClick={() => setUploadOpen(true)}>导入 ZIP</Button>
      <Button type="primary" icon={<Plus size={16} />} onClick={() => void openEditor("new")}>新建 Skill</Button>
    </>} />
    <section className="resource-surface" aria-label="Skill 列表">
      <div className="resource-toolbar"><div className="resource-filters">
        <Input aria-label="搜索 Skill" placeholder="搜索名称、描述或分组" prefix={<Search size={16} />} allowClear value={query} onChange={(event) => setQuery(event.target.value)} />
        <Select aria-label="Skill 归属" value={scope} onChange={setScope} options={[{ value: "all", label: "全部可见" }, { value: "mine", label: "我的 Skill" }, { value: "public", label: "他人公开" }]} />
        <Select aria-label="Skill 分组" value={group} onChange={setGroup} options={[{ value: "all", label: "全部分组" }, ...groups.map((value) => ({ value, label: value }))]} />
      </div><Space><span className="resource-count">{filtered.length} / {items.length} 个 Skill</span><Button icon={<RefreshCw size={16} />} loading={loading} aria-label="刷新" onClick={() => void reload()} /></Space></div>
      {error && <Alert className="resource-alert" type="error" showIcon message={error} action={<Button size="small" onClick={() => void reload()}>重试</Button>} />}
      {(!error || items.length > 0) && <ResourceCollection items={filtered} rowKey="id" loading={loading} scrollX={1240} empty={<Empty description={items.length ? "没有匹配的 Skill，请调整筛选条件" : "创建或导入第一个 Skill，让 Agent 复用你的经验"} />}
        columns={[
          { title: "名称", dataIndex: "name", width: 220, ellipsis: true, render: (name: string) => <span className="resource-title-cell"><FileArchive size={18} /><strong>{name}</strong></span> },
          { title: "描述", dataIndex: "description", width: 320, ellipsis: { showTitle: false },
            render: (description: string) => description ? <Tooltip title={description} trigger={["hover", "focus"]}
              styles={{ root: { maxWidth: "min(480px, calc(100vw - 32px))" }, body: { maxHeight: "60vh", overflowY: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" } }}>
              <span tabIndex={0} style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{description}</span>
            </Tooltip> : "暂无用途说明" },
          { title: "分组", dataIndex: "category", width: 120, ellipsis: true, render: (value: string | null) => value || "未分组" },
          { title: "状态", width: 180, render: (_: unknown, item: Skill) => <div className="resource-tags">{tags(item)}</div> },
          { title: "版本", dataIndex: "current_version", width: 80, render: (value: number) => `v${value}` },
          { title: "操作", width: 320, fixed: "right", render: (_: unknown, item: Skill) => actions(item) },
        ]} renderCard={(item) => <ResourceCard icon={<FileArchive size={20} />} title={item.name} description={item.description} tags={tags(item)} meta={`${item.category || "未分组"} · v${item.current_version} · ${item.owner_id === userId ? "由我创建" : "他人公开"}`} actions={actions(item)} />} />}
    </section>
    <Drawer title={editing === "new" ? "新建 Skill" : "编辑 Skill"} rootClassName="resource-editor" width="min(100vw, var(--yp-editor-width))" open={editing !== null} onClose={() => { if (!busy) setEditing(null); }}
      extra={<Button type="primary" loading={busy} disabled={!content.trim()} onClick={() => void save()}>保存</Button>}>
      <Space direction="vertical" size="middle" className="resource-full-width">
        {editorError && <Alert type="error" showIcon message={editorError} description="内容已保留，请检查后重新保存。" />}
        <Alert type="info" showIcon message="名称与描述从 SKILL.md 的元信息中读取" />
        <label htmlFor="skill-category">分组<Input id="skill-category" value={category} maxLength={64} onChange={(event) => setCategory(event.target.value)} /></label>
        <label>可见性<Select aria-label="可见性" style={{ width: "100%" }} value={visibility} onChange={setVisibility} options={[{ value: "private", label: "私有" }, { value: "public", label: "公开" }]} /></label>
        {editing !== "new" && <label className="resource-switch-row">启用<Switch aria-label="启用 Skill" checked={enabled} onChange={setEnabled} /></label>}
        <label htmlFor="skill-content">SKILL.md<Input.TextArea id="skill-content" aria-label="SKILL.md" value={content} rows={19} spellCheck={false} className="resource-code" onChange={(event) => setContent(event.target.value)} /></label>
        {editing && editing !== "new" ? <section className="skill-report-template" aria-label="报告模板">
          <Typography.Title level={5}>Skill 产物模板</Typography.Title>
          <Alert type="info" showIcon message="由 Skill 正文说明何时使用模板" description="例如：先调用 func1 工具获取数据，再按照 xxx_template.docx 生成报告。请上传正文引用的 DOCX 模板并定义报告字段；模型读取 Skill 时会同时获得模板文件名和字段契约，对话中无需选择报告模式。上传或替换会发布新版本，需在 Agent 中重新关联 Skill；文字 ZIP 不包含模板。" />
          <Typography.Paragraph type="secondary">每个 Skill 最多 10 个 DOCX 模板，每个最多 2 MiB。可一次选择多个，逐个设置字段；同名文件替换当前模板，其他文件保留。多模板时，Skill 正文应明确使用哪个模板或说明选择规则。</Typography.Paragraph>
          <div className="skill-template-list">
            {templatesOf(editing).map(item => <div className="skill-template-card" key={item.artifact_id}>
              <div className="skill-template-head"><Typography.Text ellipsis title={item.filename}>{item.filename}</Typography.Text><Tag color="green">已发布</Tag>
                <Button icon={<Download size={16} />} aria-label={`下载模板 ${item.filename}`} onClick={() => { void downloadArtifact(item.artifact_id, item.filename).catch(failure => message.error(failure instanceof Error ? failure.message : "下载失败")); }}>下载模板</Button></div>
              <Typography.Text type="secondary">字段：{Object.keys((item.fields_schema.properties || {}) as Record<string, unknown>).join("、") || "未定义"}</Typography.Text>
            </div>)}
          </div>
          <Upload accept=".docx" multiple fileList={[]} beforeUpload={(file, incoming) => { if (file.uid === incoming[0]?.uid) addTemplates(incoming); return false; }} disabled={busy}>
            <Button icon={<UploadIcon size={16} />} disabled={busy}>选择 DOCX 模板（可多选）</Button>
          </Upload>
          <Typography.Paragraph type="secondary">在 Word 中使用 {"{{ title }}"}、{"{{ summary }}"} 等字段，可使用条件和单层列表循环。支持正文、表格及页眉页脚；不允许宏、嵌入对象或外部资源。</Typography.Paragraph>
          <div className="skill-template-list">
            {templateDrafts.map(item => <div className="skill-template-card" key={item.id}>
              <div className="skill-template-head"><Typography.Text ellipsis title={item.file.name}>{item.file.name}</Typography.Text>
                <Tag color={templatesOf(editing).some(previous => templateKey(previous.filename) === templateKey(item.file.name)) ? "orange" : "blue"}>{templatesOf(editing).some(previous => templateKey(previous.filename) === templateKey(item.file.name)) ? "替换同名模板" : "新增模板"}</Tag>
                <Button type="text" danger icon={<Trash2 size={15} />} aria-label={`移除待发布模板 ${item.file.name}`} disabled={busy} onClick={() => setTemplateDrafts(current => current.filter(entry => entry.id !== item.id))} />
              </div>
              <label htmlFor={`template-schema-${item.id}`}>报告字段 JSON Schema<Input.TextArea id={`template-schema-${item.id}`} aria-label={`报告字段 JSON Schema ${item.file.name}`} className="resource-code" value={item.schema} rows={7} onChange={event => setTemplateDrafts(current => current.map(entry => entry.id === item.id ? { ...entry, schema: event.target.value } : entry))} disabled={busy} /></label>
            </div>)}
          </div>
          {unsaved && <Typography.Paragraph type="warning">请先保存 Skill 正文与设置，再发布模板；已选文件与字段会保留。</Typography.Paragraph>}
          <Button type="primary" loading={busy} disabled={!templateDrafts.length || Boolean(unsaved)} onClick={() => void publishTemplate()}>发布模板新版本{templateDrafts.length ? `（${templateDrafts.length} 个）` : ""}</Button>
        </section> : <Typography.Paragraph type="secondary">保存 Skill 后可上传 DOCX 报告模板。</Typography.Paragraph>}
        {editing && editing !== "new" && Object.keys(editing.files || {}).length > 1 && <Typography.Text type="secondary">附加文件保留在当前包中。</Typography.Text>}
      </Space>
    </Drawer>
    <Modal title="导入 Skill ZIP" open={uploadOpen} closable={!busy} maskClosable={!busy} onCancel={() => { if (!busy) setUploadOpen(false); }} footer={null}>
      <Space direction="vertical" size="middle" style={{ width: "100%" }}>
        <Select aria-label="导入可见性" className="resource-full-width" value={uploadVisibility} onChange={setUploadVisibility} options={[{ value: "keep", label: "保留现有可见性；新建时私有" }, { value: "private", label: "私有" }, { value: "public", label: "公开" }]} />
        <Upload.Dragger accept=".zip,application/zip" maxCount={1} showUploadList={false} disabled={busy} beforeUpload={(file) => { void doImport(file); return false; }}>
          <FilePlus2 size={24} /><p>{busy ? "正在导入，请稍候…" : "点击或拖拽 Skill ZIP 到这里"}</p><p className="resource-muted">ZIP 不超过 1 MiB，需包含 SKILL.md。</p>
        </Upload.Dragger>
      </Space>
    </Modal>
  </div>;
}
