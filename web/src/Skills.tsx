import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Empty, Input, Modal, Popconfirm, Select, Space, Switch, Tag, Typography, Upload, message } from "antd";
import { Download, FileArchive, FilePlus2, Pencil, Plus, RefreshCw, Search, Trash2, Upload as UploadIcon } from "lucide-react";
import { createSkill, deleteSkill, downloadSkill, getSkill, importSkill, listSkills, updateSkill, type Skill } from "./api";

import { PageHeader, ResourceCard, ResourceCollection } from "./components/ResourcePage";

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
    setEditorError("");
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
        await updateSkill(editing.id, { expected_version: editing.current_version,
          files: { ...editing.files, "SKILL.md": content }, category: category || null, visibility, enabled });
      }
      message.success("Skill 已保存"); setEditing(null); await reload();
    } catch (failure) { setEditorError(failure instanceof Error ? failure.message : "保存失败"); }
    finally { setBusy(false); }
  }

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
      {(!error || items.length > 0) && <ResourceCollection items={filtered} rowKey="id" loading={loading} empty={<Empty description={items.length ? "没有匹配的 Skill，请调整筛选条件" : "创建或导入第一个 Skill，让 Agent 复用你的经验"} />}
        columns={[
          { title: "名称", dataIndex: "name", render: (name: string) => <span className="resource-title-cell"><FileArchive size={18} /><strong>{name}</strong></span> },
          { title: "描述", dataIndex: "description", ellipsis: true },
          { title: "分组", dataIndex: "category", render: (value: string | null) => value || "未分组" },
          { title: "状态", render: (_: unknown, item: Skill) => <div className="resource-tags">{tags(item)}</div> },
          { title: "版本", dataIndex: "current_version", render: (value: number) => `v${value}` },
          { title: "操作", render: (_: unknown, item: Skill) => actions(item) },
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
