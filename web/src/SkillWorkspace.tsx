import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, Button, Empty, Grid, Input, Modal, Popconfirm, Segmented, Select, Space, Spin, Switch, Tabs, Tag, Tree, Typography, message } from "antd";
import type { DataNode } from "antd/es/tree";
import { ArrowLeft, CheckCircle2, Download, File, FilePlus2, Folder, Play, RefreshCw, Save, Upload as UploadIcon } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import { downloadSkillFile, exportSkillVersion, getSkillWorkspace, importSkillPackage, previewSkillFile, publishSkillDraft, readSkillFile, saveSkillDraft, startSkillDraft, testSkillScript, uploadSkillFiles, validateSkillDraft, type ArtifactPreview, type SkillDraft, type SkillFile, type SkillValidation, type SkillWorkspaceData } from "./api";
import { PageHeader } from "./components/ResourcePage";
import { CopyButton, MessageContent } from "./components/MessageContent";
import { PreviewContent } from "./components/ArtifactPreview";
import "./styles/skill-workspace.css";

const textPath = (path: string) => /\.(md|txt|py|sh|json|ya?ml|csv)$/i.test(path);
const errorText = (failure: unknown) => failure instanceof Error ? failure.message : "操作失败，请重试";
const configExample = JSON.stringify({ templates: { "assets/templates/report.docx": "assets/schemas/report.schema.json" }, scripts: {}, required_tools: [] }, null, 2);
function treeOf(files: SkillFile[]): DataNode[] {
  const root: DataNode[] = [];
  for (const file of files) {
    let nodes = root; let prefix = "";
    const parts = file.path.split("/");
    parts.forEach((part, index) => {
      prefix += (prefix ? "/" : "") + part;
      let node = nodes.find((item) => item.key === prefix);
      if (!node) { const leaf = index === parts.length - 1; node = { key: prefix, title: <span title={prefix}>{part}</span>, isLeaf: leaf, selectable: leaf, icon: leaf ? <File size={15} /> : <Folder size={15} />, ...(leaf ? {} : { children: [] }) }; nodes.push(node); }
      if (node.children) nodes = node.children;
    });
  }
  return root;
}
export default function SkillWorkspace() {
  const screens = Grid.useBreakpoint();
  const { skillId } = useParams(); const id = Number(skillId); const navigate = useNavigate();
  const [workspace, setWorkspace] = useState<SkillWorkspaceData | null>(null);
  const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [tab, setTab] = useState("files"); const [version, setVersion] = useState<number | undefined>();
  const [path, setPath] = useState("SKILL.md"); const [source, setSource] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [fileLoading, setFileLoading] = useState(false); const [fileError, setFileError] = useState(""); const [preview, setPreview] = useState<ArtifactPreview | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({}); const [mode, setMode] = useState("编辑");
  const [settings, setSettings] = useState<SkillDraft["settings"] | null>(null);
  const [report, setReport] = useState<SkillValidation | null>(null); const [configText, setConfigText] = useState("");
  const [newPath, setNewPath] = useState(""); const [pathDialog, setPathDialog] = useState<"new" | "rename" | null>(null);
  const [scriptPath, setScriptPath] = useState<string>(); const [argumentsText, setArgumentsText] = useState("{}"); const [testResult, setTestResult] = useState("");
  const [compare, setCompare] = useState<{ version: number; content: string } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null); const directoryInput = useRef<HTMLInputElement>(null); const zipInput = useRef<HTMLInputElement>(null);
  const [uploadDir, setUploadDir] = useState("references");
  const readonly = workspace?.readonly || version !== undefined;
  const draft = workspace?.draft;
  const settingsDirty = !!draft && JSON.stringify(settings) !== JSON.stringify(draft.settings);
  const dirty = Object.keys(edits).length > 0 || settingsDirty;
  const fixed = workspace?.versions.find((item) => item.version === version);
  const files = useMemo(() => {
    const manifest = version === undefined ? draft?.files || [] : (fixed?.manifest || []).filter((item) => !("report_template" in item));
    const entries = new Map(manifest.map((item) => [item.path, item]));
    if (version === undefined) Object.keys(edits).forEach((key) => { if (!entries.has(key)) entries.set(key, { path: key, bytes: new Blob([edits[key]]).size, sha256: "", is_text: true }); });
    return [...entries.values()].sort((a, b) => a.path.localeCompare(b.path));
  }, [version, draft, fixed, edits]);
  const selected = files.find((item) => item.path === path);
  const isText = selected?.is_text ?? textPath(path);
  const content = version === undefined && path in edits ? edits[path] : source;
  const editable = !readonly && !!draft;
  const applyDraft = useCallback((next: SkillDraft) => {
    setWorkspace((current) => current ? { ...current, draft: next } : current); setSettings(next.settings); setReport(null);
  }, []);
  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const data = await getSkillWorkspace(id);
      if (!data.readonly && !data.draft) data.draft = await startSkillDraft(id);
      setWorkspace(data); setSettings(data.draft?.settings || null);
      if (data.readonly) { setVersion(data.skill.current_version); setMode("预览"); }
    } catch (failure) { setError(errorText(failure)); }
    finally { setLoading(false); }
  }, [id]);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { directoryInput.current?.setAttribute("webkitdirectory", ""); }, [loading, tab]);
  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) { if (dirty) { event.preventDefault(); event.returnValue = ""; } }
    window.addEventListener("beforeunload", beforeUnload); return () => window.removeEventListener("beforeunload", beforeUnload);
  }, [dirty]);
  useEffect(() => {
    let active = true; let image = "";
    setImageUrl("");
    setFileError(""); setPreview(null); setSource("");
    if (!selected || (version === undefined && !draft?.files.some((item) => item.path === path))) { setFileLoading(false); return; }
    setFileLoading(true);
    void (/^image\/(png|jpeg)$/.test(selected.media_type || "") ? readSkillFile(id, path, version).then((blob) => { if (active) { image = URL.createObjectURL(blob); setImageUrl(image); } }) : isText ? readSkillFile(id, path, version).then((blob) => blob.text()).then((text) => { if (active) setSource(text); })
      : previewSkillFile(id, path, version).then((result) => { if (active) setPreview(result); })).catch((failure) => { if (active) setFileError(errorText(failure)); }).finally(() => { if (active) setFileLoading(false); });
    return () => { active = false; if (image) URL.revokeObjectURL(image); };
  }, [id, path, version, draft?.revision, isText, selected?.sha256]);
  async function perform(action: () => Promise<void>) {
    if (busy) return; setBusy(true); setError("");
    try { await action(); } catch (failure) { setError(errorText(failure)); } finally { setBusy(false); }
  }
  async function save(): Promise<SkillDraft> {
    if (!draft) throw new Error("草稿不存在");
    if (!dirty) return draft;
    const next = await saveSkillDraft(id, { expected_revision: draft.revision, files: edits, ...settings! });
    applyDraft(next); if (path in edits) setSource(edits[path]); setEdits({}); return next;
  }
  async function validate() {
    const next = await save(); const result = await validateSkillDraft(id, next.revision); setReport(result);
    setConfigText(JSON.stringify(result.config, null, 2));
    setScriptPath((current) => current && result.config.scripts?.[current] ? current : Object.keys(result.config.scripts || {})[0]);
    return result;
  }
  function guardDiscard(action: () => void) {
    if (!dirty) { action(); return; }
    Modal.confirm({ title: "有未保存的修改", content: "离开当前草稿视图会丢弃未保存的修改。", okText: "放弃修改", cancelText: "继续编辑", onOk: () => { setEdits({}); setSettings(draft?.settings || null); action(); } });
  }
  async function upload(incoming: FileList | null, folder = false) {
    if (!incoming?.length) return;
    const all = [...incoming];
    if (all.length > 128 || all.reduce((total, file) => total + file.size, 0) > 32 * 1024 * 1024) { setError("最多上传 128 个文件，总大小不超过 32 MiB"); return; }
    const root = folder && all.some((file) => /^[^/]+\/SKILL\.md$/.test(file.webkitRelativePath));
    await perform(async () => {
      const next = await save();
      const result = await uploadSkillFiles(id, next.revision, all.map((file) => ({ file, path: folder ? (root ? file.webkitRelativePath.split("/").slice(1).join("/") : file.webkitRelativePath) : (uploadDir ? `${uploadDir}/` : "") + file.name })));
      applyDraft(result); message.success(`${all.length} 个文件已保存到草稿`);
    });
  }
  if (loading && !workspace) return <div className="skill-workspace-loading"><Spin tip="加载 Skill 工作区" /></div>;
  if (!workspace) return <Alert type="error" showIcon message={error || "Skill 不存在"} action={<Button onClick={() => void reload()}>重试</Button>} />;
  const runScripts = Object.keys(report?.config.scripts || {});
  const sections = [{ value: "files", label: "文件工作区" }, { value: "overview", label: "概览与设置" }, { value: "execution", label: "执行与资源配置" }, { value: "test", label: "脚本试运行" }, { value: "versions", label: "版本历史" }];
  function changeTab(key: string) {
    setTab(key);
    if ((key === "execution" || key === "test") && editable && !report) void perform(async () => { await validate(); });
  }
  return <div className="skill-workspace resource-page">
    <PageHeader title={workspace.skill.name} description={workspace.skill.description} actions={<>
      <Button icon={<ArrowLeft size={16} />} onClick={() => guardDiscard(() => navigate("/skills"))}>Skill 列表</Button>
      <Button disabled={busy} icon={<RefreshCw size={16} />} onClick={() => guardDiscard(() => void reload())}>刷新</Button>
      {editable && <><Button icon={<Save size={16} />} loading={busy} onClick={() => void perform(async () => { await save(); message.success("草稿已保存"); })}>保存草稿</Button>
        <Button icon={<CheckCircle2 size={16} />} disabled={busy} onClick={() => void perform(async () => { await validate(); })}>校验</Button>
        <Button type="primary" aria-label="发布 Skill" disabled={busy} onClick={() => void perform(async () => {
          const result = await validate(); if (!result.valid) { setTab("execution"); return; }
          Modal.confirm({ title: "发布当前 Skill 文件包？", content: `发布为 v${workspace.skill.current_version + 1}，Agent 已关联的版本继续固定。需要使用新版时，在 Agent 配置中重新关联此 Skill。`, okText: "发布版本", onOk: async () => {
            await perform(async () => { await publishSkillDraft(id, result.revision); await reload(); message.success("完整文件包已发布"); });
          } });
        })}>发布</Button></>}
    </>} />
    <div className="skill-workspace-status"><Space wrap><Tag color={workspace.skill.current_version ? "blue" : "orange"}>{workspace.skill.current_version ? `已发布 v${workspace.skill.current_version}` : "尚未发布"}</Tag>
      {draft && <Tag color={dirty ? "orange" : "cyan"}>{dirty ? "有未保存修改" : `草稿 r${draft.revision}`}</Tag>}
      <span>{files.length} 个文件 · {(files.reduce((sum, file) => sum + file.bytes, 0) / 1024).toFixed(1)} KiB</span></Space>
      <Select aria-label="工作区版本" value={version || "draft"} disabled={busy} onChange={(value) => guardDiscard(() => { setVersion(value === "draft" ? undefined : Number(value)); setPath("SKILL.md"); setReport(null); })} options={[...(!workspace.readonly ? [{ value: "draft", label: "草稿工作区" }] : []), ...workspace.versions.map((item) => ({ value: item.version, label: `发布版本 v${item.version}` }))]} />
    </div>
    {error && <Alert type="error" message={error} showIcon closable onClose={() => setError("")} />}
    {report && <Alert type={report.valid ? "success" : "warning"} showIcon message={report.valid ? "校验通过，可以发布" : report.errors.length ? `发布校验发现 ${report.errors.length} 个问题` : "配置已修改，保存并校验后生效"} description={<>{report.errors.map((item) => <p key={item}>{item}</p>)}{report.warnings.map((item) => <p key={item}>{item}</p>)}</>} />}
    {readonly && <Alert type="info" showIcon message={workspace.readonly ? "正在查看公开的发布版本" : `正在查看固定发布版本 v${version}`} description={workspace.readonly ? "公开资源可查看和导出，草稿与历史版本仅所有者可见。" : "回到草稿继续编辑，或在版本历史中恢复此版本到草稿。"} />}
    <Tabs activeKey={tab} onChange={changeTab} renderTabBar={!screens.md ? () => <div className="skill-mobile-sections"><Select aria-label="Skill 工作区栏目" value={tab} options={sections} onChange={changeTab} /></div> : undefined} items={[
      { key: "files", label: "文件工作区", children: <>
        {editable && <div className="skill-file-toolbar"><Space wrap>
          <Button icon={<FilePlus2 size={16} />} disabled={busy} onClick={() => { setNewPath(uploadDir ? `${uploadDir}/new.md` : "new.md"); setPathDialog("new"); }}>新建文件</Button>
          <Select aria-label="上传目标目录" value={uploadDir} onChange={setUploadDir} options={[{ value: "", label: "根目录" }, { value: "references", label: "references/" }, { value: "scripts", label: "scripts/" }, { value: "assets/templates", label: "assets/templates/" }, { value: "assets/schemas", label: "assets/schemas/" }, { value: "assets/examples", label: "assets/examples/" }]} />
          <Button icon={<UploadIcon size={16} />} disabled={busy} onClick={() => fileInput.current?.click()}>上传文件</Button>
          <Button disabled={busy} onClick={() => directoryInput.current?.click()}>上传目录</Button>
          <Button disabled={busy} onClick={() => Modal.confirm({ title: "用 ZIP 替换整个草稿？", content: "发布版本保持固定，ZIP 将替换当前全部草稿文件。", okText: "选择 ZIP", onOk: () => zipInput.current?.click() })}>导入 ZIP</Button>
        </Space></div>}
        <input hidden ref={fileInput} type="file" multiple onChange={(event) => { void upload(event.target.files); event.target.value = ""; }} />
        <input hidden ref={directoryInput} type="file" multiple onChange={(event) => { void upload(event.target.files, true); event.target.value = ""; }} />
        <input hidden ref={zipInput} type="file" accept=".zip" onChange={(event) => { const file = event.target.files?.[0]; event.target.value = ""; if (file) void perform(async () => { const next = await save(); const result = await importSkillPackage(file, id, next.revision) as SkillDraft; applyDraft(result); setPath("SKILL.md"); }); }} />
        <div className="skill-file-workbench">
          <aside className="skill-file-tree" aria-label="Skill 文件树"><Tree showIcon blockNode defaultExpandAll key={`${version || "draft"}-${files.length}`} selectedKeys={[path]} treeData={treeOf(files)} onSelect={(keys) => { if (keys.length) setPath(String(keys[0])); }} /></aside>
          <section className="skill-file-editor" aria-label="文件编辑与预览">
            <div className="skill-editor-header"><Typography.Text code ellipsis title={path}>{path}</Typography.Text><Space wrap>
              {isText && <CopyButton text={content} label="复制文件内容" />}
              {isText && <Segmented aria-label="文件显示方式" value={readonly ? "预览" : mode} options={readonly ? ["预览"] : ["编辑", "预览"]} onChange={setMode} />}
              <Button size="small" aria-label="下载当前文件" icon={<Download size={15} />} disabled={!selected?.sha256 || busy} onClick={() => void perform(async () => { await downloadSkillFile(id, path, version); })} />
              {editable && path !== "SKILL.md" && <><Button size="small" disabled={busy} onClick={() => { setNewPath(path); setPathDialog("rename"); }}>重命名</Button><Popconfirm title={`从草稿删除 ${path}？`} onConfirm={() => void perform(async () => {
                const next = await save(); applyDraft(await saveSkillDraft(id, { expected_revision: next.revision, delete_paths: [path] })); setPath("SKILL.md");
              })}><Button size="small" danger disabled={busy}>删除</Button></Popconfirm></>}
            </Space></div>
            {fileLoading ? <div className="skill-workspace-loading"><Spin /></div> : fileError ? <Alert type="info" showIcon message={fileError} description="可使用下载按钮查看原件。" /> : imageUrl ? <div className="skill-file-preview"><img className="skill-image-preview" src={imageUrl} alt={path} /></div> : isText ? (editable && mode === "编辑" ?
              <Input.TextArea className="skill-source-editor" aria-label={`编辑 ${path}`} value={content} spellCheck={false} disabled={busy} onChange={(event) => { const value = event.target.value; setEdits((current) => ({ ...current, [path]: value })); setReport(null); }} />
              : <div className="skill-file-preview">{/\.md$/i.test(path) ? <MessageContent text={content} /> : <pre>{content}</pre>}</div>) : preview ? <div className="skill-file-preview">{preview.notice && <Alert type="info" message={preview.notice} />}{preview.truncated && <Tag color="orange">内容已截断</Tag>}<PreviewContent preview={preview} /></div> : <Empty description="选择一个文件以查看内容" />}
          </section>
        </div>
      </> },
      { key: "overview", label: "概览与设置", children: <section className="skill-workspace-panel">
        <h3>一个文件包，一组可复用能力</h3><p>SKILL.md 描述适用场景和步骤；references 保存参考资料；scripts 声明后在沙箱调用；assets 保存模板、字段契约和示例。</p>
        <Alert type="info" showIcon message="Agent 关联发布版本，Run 再固定本轮使用的版本" description="编辑草稿不会影响对话。用户在对话中直接提出任务，模型根据 Skill 正文读取资料、调用工具并生成产物。" />
        <label className="resource-field"><span>分组</span><Input aria-label="Skill 分组" disabled={!editable || busy} value={settings?.category || ""} maxLength={64} onChange={(event) => setSettings((current) => current ? { ...current, category: event.target.value || null } : current)} /></label>
        <label className="resource-field"><span>发布后的可见性</span><Select aria-label="Skill 可见性" disabled={!editable || busy} value={settings?.visibility || workspace.skill.visibility} options={[{ value: "private", label: "私有" }, { value: "public", label: "公开" }]} onChange={(value) => setSettings((current) => current ? { ...current, visibility: value } : current)} /></label>
        <label className="resource-field"><span>发布后启用</span><Switch aria-label="发布后启用" disabled={!editable || busy} checked={settings?.enabled ?? workspace.skill.enabled} onChange={(value) => setSettings((current) => current ? { ...current, enabled: value } : current)} /></label>
        <p>修改设置需与草稿一起发布。公开 Skill 允许其他用户查看、下载和关联当前版本。</p>
      </section> },
      { key: "execution", label: "执行与资源配置", children: <section className="skill-workspace-panel">
        <h3>模板、脚本与外部工具</h3><p>在 y-point.yaml 声明模板对应的字段 Schema、脚本输入及固定依赖、必需的外部工具。JSON 也是合法的 YAML。</p>
        {editable && files.some((file) => file.path.startsWith("assets/templates/") && file.path.endsWith(".docx")) && <div className="skill-template-bindings"><h4>产物模板与字段契约</h4><p>每个模板独立关联一个 Schema。模型根据 Skill 正文选用模板。</p>
          {files.filter((file) => file.path.startsWith("assets/templates/") && file.path.endsWith(".docx")).map((file) => <label className="resource-field" key={file.path}><span>{file.path}</span>
            <Select aria-label={`${file.path} 字段契约`} allowClear disabled={busy || !report} placeholder="选择 assets/schemas 中的 JSON 文件" value={report?.config.templates?.[file.path]} options={files.filter((item) => item.path.startsWith("assets/schemas/") && item.path.endsWith(".json")).map((item) => ({ value: item.path, label: item.path }))} onChange={(schema: string | undefined) => {
              if (!report) return; const templates = { ...report.config.templates }; if (schema) templates[file.path] = schema; else delete templates[file.path];
              const config = { ...report.config, templates }; const value = JSON.stringify(config, null, 2); setConfigText(value); setEdits((current) => ({ ...current, "y-point.yaml": value }));
              setReport({ ...report, config, valid: false, errors: [], warnings: ["模板关联已修改，请保存配置并校验"] });
            }} />
          </label>)}
        </div>}
        {editable ? <><Input.TextArea aria-label="Skill 执行配置" className="skill-config-editor" rows={16} disabled={busy} value={edits["y-point.yaml"] ?? configText} placeholder={configExample} onChange={(event) => { const value = event.target.value; setConfigText(value); setEdits((current) => ({ ...current, "y-point.yaml": value })); setReport(null); }} />
          <Space wrap><Button disabled={busy} onClick={() => { if (configText) return; setConfigText(configExample); setEdits((current) => ({ ...current, "y-point.yaml": configExample })); }}>填入模板关联示例</Button><Button type="primary" disabled={busy} onClick={() => void perform(async () => { await validate(); })}>保存配置并校验</Button></Space></> : <p>请在文件工作区查看 y-point.yaml。</p>}
        <div className="skill-contracts">{Object.entries(report?.config.templates || {}).map(([template, schema]) => <div key={template}><Tag color="purple">模板</Tag><Button type="link" onClick={() => { setPath(template); setTab("files"); }}>{template}</Button><span>字段契约：{schema}</span></div>)}
          {(report?.config.required_tools || []).map((name) => <Tag key={name} color="blue">必需工具：{name}</Tag>)}</div>
        <Alert type="info" showIcon message="外部工具需在 Agent 中显式关联" description="脚本只读挂载此包、默认无网络，执行能力与批准规则复用 Tool；脚本返回 JSON，文档产物交给模板生成工具。" />
      </section> },
      { key: "test", label: "脚本试运行", children: <section className="skill-workspace-panel">
        <h3>验证完整包中的脚本</h3><p>选择已声明脚本，准备依赖后试运行。完整包任一文件变化后需要重新验证。</p>
        {!editable ? <Empty description="请回到草稿试运行脚本" /> : runScripts.length ? <>
          <Select aria-label="选择包内脚本" value={scriptPath} onChange={(value) => { setScriptPath(value); setTestResult(""); }} options={runScripts.map((value) => ({ value, label: value }))} />
          <div className="skill-script-status">{report?.scripts?.map((item) => <Space key={item.path} wrap><span>{item.path}</span><Tag color={item.verified ? "success" : "orange"}>{item.verified ? "已验证" : "待试运行"}</Tag><span>{item.environment?.message || ({ ready: "依赖已就绪", not_required: "无需额外依赖", pending: "依赖等待准备", building: "依赖准备中", failed: "依赖准备失败" }[item.environment?.state || ""] || "尚未准备")}</span></Space>)}</div>
          <label className="resource-field"><span>输入参数 JSON</span><Input.TextArea aria-label="脚本输入参数" rows={6} value={argumentsText} onChange={(event) => setArgumentsText(event.target.value)} /></label>
          <Space wrap><Button disabled={busy || !scriptPath} onClick={() => void perform(async () => {
            const next = await save(); const result = await testSkillScript(id, next.revision, scriptPath!, {}, true); setTestResult(JSON.stringify(result, null, 2)); await validate();
          })}>准备 / 刷新依赖</Button><Button type="primary" icon={<Play size={15} />} disabled={busy || !scriptPath} onClick={() => void perform(async () => {
            const next = await save(); const args: unknown = JSON.parse(argumentsText); if (!args || typeof args !== "object" || Array.isArray(args)) throw new Error("输入参数必须为 JSON 对象");
            const result = await testSkillScript(id, next.revision, scriptPath!, args as Record<string, unknown>); setTestResult(result.result || "试运行通过"); await validate();
          })}>试运行脚本</Button></Space>{testResult && <pre className="skill-test-result">{testResult}</pre>}
        </> : <Empty description="尚未声明可执行脚本，请先在执行配置中添加 scripts" />}
      </section> },
      { key: "versions", label: "版本历史", children: <section className="skill-workspace-panel">{workspace.versions.length ? workspace.versions.map((item) => <div className="skill-version-row" key={item.version}>
        <div><strong>v{item.version}</strong>{item.version === workspace.skill.current_version && <Tag color="blue">当前发布</Tag>}<p>{item.create_time}</p><Typography.Text code copyable>{item.content_hash}</Typography.Text></div>
        <Space wrap><Button disabled={busy} onClick={() => guardDiscard(() => { setVersion(item.version); setPath("SKILL.md"); setTab("files"); })}>查看文件</Button>
          <Button disabled={busy} icon={<Download size={15} />} onClick={() => void perform(async () => { await exportSkillVersion(id, item.version); })}>导出 ZIP</Button>
          {!workspace.readonly && <><Button disabled={busy} onClick={() => void perform(async () => {
            const blob = await readSkillFile(id, "SKILL.md", item.version); setCompare({ version: item.version, content: await blob.text() });
          })}>比较正文</Button><Popconfirm title={`将 v${item.version} 恢复到草稿？`} description="当前草稿将被替换，已发布版本和 Agent 的固定引用保持不变。" onConfirm={() => void perform(async () => {
            applyDraft(await startSkillDraft(id, item.version, draft?.revision)); setEdits({}); setVersion(undefined); setPath("SKILL.md"); setTab("files"); message.success("历史版本已恢复到草稿，校验发布后生效");
          })}><Button disabled={busy}>恢复到草稿</Button></Popconfirm></>}
        </Space>
      </div>) : <Empty description="尚未发布第一个版本" />}</section> },
    ]} />
    <Modal title={pathDialog === "rename" ? "重命名草稿文件" : "新建文本文件"} open={!!pathDialog} onCancel={() => setPathDialog(null)} confirmLoading={busy} okText="保存到草稿" onOk={() => void perform(async () => {
      if (!newPath || newPath.startsWith("/") || newPath.split("/").some((part) => !part || part.startsWith("."))) throw new Error("请填写包内相对路径，例如 references/policy.md");
      if (pathDialog === "new" && files.some((item) => item.path.toLowerCase() === newPath.toLowerCase())) throw new Error("该路径已存在");
      if (pathDialog === "new" && !textPath(newPath)) throw new Error("仅支持新建文本文件，二进制资源请上传");
      const next = await save(); applyDraft(await saveSkillDraft(id, { expected_revision: next.revision, ...(pathDialog === "rename" ? { renames: { [path]: newPath } } : { files: { [newPath]: "" } }) })); setPath(newPath); setMode("编辑"); setPathDialog(null);
    })}><label className="resource-field"><span>包内路径</span><Input aria-label="新文件路径" value={newPath} onChange={(event) => setNewPath(event.target.value)} /></label><p>核心文件 SKILL.md 保留在根目录。文件夹会随路径自动创建。</p></Modal>
    <Modal title={`正文比较 · v${compare?.version}`} open={!!compare} onCancel={() => setCompare(null)} footer={null} width="min(1100px, calc(100vw - 24px))">
      <div className="skill-version-compare"><section><h4>发布版本 v{compare?.version}</h4><pre>{compare?.content}</pre></section><section><h4>当前草稿正文</h4><DraftText id={id} revision={draft?.revision} override={edits["SKILL.md"]} /></section></div>
    </Modal>
  </div>;
}
function DraftText({ id, revision, override }: { id: number; revision?: number; override?: string }) {
  const [text, setText] = useState("");
  useEffect(() => { let active = true; void readSkillFile(id, "SKILL.md").then((blob) => blob.text()).then((value) => { if (active) setText(value); }).catch((failure) => { if (active) setText(errorText(failure)); }); return () => { active = false; }; }, [id, revision]);
  return <pre>{override ?? text}</pre>;
}
