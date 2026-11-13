import { useEffect, useState } from "react";
import { Alert, Button, Empty, Input, Modal, Popconfirm, Select, Space, Tag, Tooltip, Upload, message } from "antd";
import { Download, FileArchive, Plus, RefreshCw, Search, Trash2, Upload as UploadIcon } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { createSkillPackage, deleteSkill, downloadSkill, importSkillPackage, listSkills, type Skill } from "./api";
import { PageHeader, ResourceCard, ResourceCollection } from "./components/ResourcePage";

export default function Skills({ userId }: { userId: number }) {
  const navigate = useNavigate();
  const [items, setItems] = useState<Skill[]>([]);
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("all");
  const [group, setGroup] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [newOpen, setNewOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  async function reload() {
    setLoading(true);
    try { setItems(await listSkills()); setError(""); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Skill 列表加载失败"); }
    finally { setLoading(false); }
  }
  useEffect(() => { void reload(); }, []);
  async function download(id?: number) {
    try { await downloadSkill(id); } catch (failure) { message.error(failure instanceof Error ? failure.message : "下载失败"); }
  }
  async function create() {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(name) || !description.trim()) { setFormError("请填写小写字母、数字和连字符组成的名称，以及适用场景"); return; }
    setBusy(true); setFormError("");
    try { const created = await createSkillPackage(name, description); navigate(`/skills/${created.id}`); }
    catch (failure) { setFormError(failure instanceof Error ? failure.message : "创建失败"); }
    finally { setBusy(false); }
  }
  const filtered = items.filter((item) => `${item.name} ${item.description} ${item.category || ""}`.toLowerCase().includes(query.toLowerCase())
    && (scope === "all" || (scope === "mine" ? item.owner_id === userId : item.owner_id !== userId)) && (group === "all" || item.category === group));
  const groups = [...new Set(items.map((item) => item.category).filter((value): value is string => Boolean(value)))];
  const tags = (item: Skill) => <><Tag>{item.visibility === "public" ? "公开" : "私有"}</Tag><Tag color={item.current_version ? "blue" : "orange"}>{item.current_version ? `v${item.current_version}` : "未发布"}</Tag>{item.current_version > 0 && <Tag color={item.enabled ? "success" : "default"}>{item.enabled ? "已启用" : "已停用"}</Tag>}</>;
  const actions = (item: Skill) => <Space size="small" wrap>
    <Button type="link" onClick={() => navigate(`/skills/${item.id}`)}>{item.owner_id === userId ? "工作区" : "查看"}</Button>
    <Tooltip title={item.current_version ? "导出完整发布版本" : "请先发布"}><Button disabled={!item.current_version} type="text" icon={<Download size={16} />} aria-label="导出" onClick={() => void download(item.id)} /></Tooltip>
    {item.owner_id === userId && <Popconfirm title="删除 Skill？" description="删除后将无法继续使用此 Skill。" onConfirm={async () => { try { await deleteSkill(item.id); await reload(); } catch (failure) { message.error(failure instanceof Error ? failure.message : "删除失败"); } }}>
      <Button type="text" danger icon={<Trash2 size={16} />} aria-label="删除" />
    </Popconfirm>}
  </Space>;
  return <div className="skills-page resource-page">
    <PageHeader title="Skill" description="将指令、参考资料、脚本和模板组织成文件包，校验发布后供 Agent 按需使用。" actions={<>
      <Button icon={<Download size={16} />} onClick={() => void download()}>下载模板</Button>
      <Button icon={<UploadIcon size={16} />} onClick={() => { setUploadOpen(true); setFormError(""); }}>导入 ZIP</Button>
      <Button type="primary" icon={<Plus size={16} />} onClick={() => { setNewOpen(true); setFormError(""); }}>新建 Skill</Button>
    </>} />
    <section className="resource-surface" aria-label="Skill 列表">
      <div className="resource-toolbar"><div className="resource-filters">
        <Input aria-label="搜索 Skill" placeholder="搜索名称、描述或分组" prefix={<Search size={16} />} allowClear value={query} onChange={(event) => setQuery(event.target.value)} />
        <Select aria-label="Skill 归属" value={scope} onChange={setScope} options={[{ value: "all", label: "全部可见" }, { value: "mine", label: "我的 Skill" }, { value: "public", label: "他人公开" }]} />
        <Select aria-label="Skill 分组" value={group} onChange={setGroup} options={[{ value: "all", label: "全部分组" }, ...groups.map((value) => ({ value, label: value }))]} />
      </div><Space><span className="resource-count">{filtered.length} / {items.length} 个 Skill</span><Button icon={<RefreshCw size={16} />} loading={loading} aria-label="刷新" onClick={() => void reload()} /></Space></div>
      {error && <Alert className="resource-alert" type="error" showIcon message={error} action={<Button size="small" onClick={() => void reload()}>重试</Button>} />}
      {(!error || items.length > 0) && <ResourceCollection items={filtered} rowKey="id" loading={loading} scrollX={1100} empty={<Empty description={items.length ? "没有匹配的 Skill，请调整筛选条件" : "创建或导入第一个 Skill，让 Agent 复用你的经验"} />}
        columns={[
          { title: "名称", dataIndex: "name", width: 220, ellipsis: true, render: (value: string, item: Skill) => <Button type="link" onClick={() => navigate(`/skills/${item.id}`)}><FileArchive size={17} />{value}</Button> },
          { title: "描述", dataIndex: "description", width: 320, ellipsis: { showTitle: false }, render: (value: string) => <Tooltip title={value} trigger={["hover", "focus"]} styles={{ root: { maxWidth: "min(480px, calc(100vw - 32px))" }, body: { maxHeight: "60vh", overflowY: "auto", whiteSpace: "pre-wrap", overflowWrap: "anywhere" } }}><span tabIndex={0} style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{value}</span></Tooltip> },
          { title: "分组", dataIndex: "category", width: 110, render: (value: string) => value || "—" },
          { title: "状态", width: 210, render: (_: unknown, item: Skill) => tags(item) },
          { title: "操作", width: 180, fixed: "right", render: (_: unknown, item: Skill) => actions(item) },
        ]}
        renderCard={(item) => <ResourceCard icon={<FileArchive size={20} />} title={item.name} meta={item.category || "未分组"} description={item.description} tags={tags(item)} actions={actions(item)} />}
      />}
    </section>
    <Modal title="新建 Skill 工作区" open={newOpen} onCancel={() => !busy && setNewOpen(false)} confirmLoading={busy} cancelText="取消" cancelButtonProps={{ disabled: busy }} okText="创建草稿" onOk={() => void create()}>
      {formError && <Alert type="error" showIcon message={formError} />}
      <label className="resource-field"><span>名称</span><Input aria-label="Skill 名称" maxLength={64} placeholder="例如：contract-review（合同审查）" value={name} onChange={(event) => setName(event.target.value)} /></label>
      <label className="resource-field"><span>适用场景</span><Input.TextArea aria-label="Skill 适用场景" placeholder="例如：审查合同条款，识别风险并生成审查报告" maxLength={1024} value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      <p>先创建私有草稿。添加文件、校验并发布后，可关联 Agent。</p>
    </Modal>
    <Modal title="导入 Skill 文件包" open={uploadOpen} onCancel={() => !busy && setUploadOpen(false)} footer={null}>
      <p>ZIP 不超过 32 MiB，包含 SKILL.md，可携带 references、scripts 和二进制 assets。导入后先保存为私有草稿。</p>
      {formError && <Alert type="error" showIcon message={formError} />}
      <Upload accept=".zip" showUploadList={false} disabled={busy} beforeUpload={(file) => {
        if (file.size > 32 * 1024 * 1024) { setFormError("ZIP 不可超过 32 MiB"); return false; }
        setBusy(true); void importSkillPackage(file).then((item) => { if ("id" in item) navigate(`/skills/${item.id}`); }).catch((failure) => setFormError(failure instanceof Error ? failure.message : "导入失败")).finally(() => setBusy(false)); return false;
      }}><Button loading={busy} icon={<UploadIcon size={16} />}>选择 ZIP</Button></Upload>
    </Modal>
  </div>;
}
