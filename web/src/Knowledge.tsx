import { useCallback, useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Empty, Input, Pagination, Select, Skeleton, Space, Tag, message } from "antd";
import { ArrowUpRight, FolderOpen, LibraryBig, Plus, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import type { User } from "./api";
import { listKnowledgeBases, type KnowledgePage } from "./knowledge-api";
import KnowledgeEditor from "./knowledge/KnowledgeEditor";
import KnowledgeWorkspace from "./knowledge/KnowledgeWorkspace";
import "./styles/knowledge.css";

export default function Knowledge({ user }: { user: User }) {
  const navigate = useNavigate();
  const { knowledgeId } = useParams();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<KnowledgePage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [scope, setScope] = useState("all");
  const [creating, setCreating] = useState(false);
  const sequence = useRef(0);
  const refresh = useCallback(async () => {
    const seq = ++sequence.current;
    setLoading(true);
    try {
      const result = await listKnowledgeBases(page);
      if (seq !== sequence.current) return;
      if (!result.items.length && page > 1 && result.total <= (page - 1) * 20) { setPage(Math.max(1, Math.ceil(result.total / 20))); return; }
      setData(result); setError("");
    } catch (failure) { if (seq === sequence.current) { setData(null); setError(failure instanceof Error ? failure.message : "知识库列表加载失败"); } }
    finally { if (seq === sequence.current) setLoading(false); }
  }, [page]);
  useEffect(() => {
    if (!knowledgeId) void refresh();
    return () => { sequence.current++; };
  }, [refresh, knowledgeId]);
  const unavailable = useCallback(() => {
    message.warning("知识库已删除或访问权限已变化，已清除当前资料");
    navigate("/knowledge-bases", { replace: true });
  }, [navigate]);
  const back = useCallback(() => navigate("/knowledge-bases"), [navigate]);
  const changed = useCallback(() => { setData(null); }, []);
  if (knowledgeId) {
    const id = Number(knowledgeId);
    if (!Number.isSafeInteger(id) || id < 1) return <Alert type="error" showIcon message="无效的知识库地址" action={<Button onClick={back}>返回列表</Button>} />;
    return <KnowledgeWorkspace key={id} id={id} user={user} onBack={back} onUnavailable={unavailable} onChanged={changed} />;
  }
  const items = (data?.items || []).filter((item) => (scope === "all" || (scope === "owned" ? item.owner_id === user.id : item.owner_id !== user.id)) && `${item.name} ${item.description || ""}`.toLowerCase().includes(query.trim().toLowerCase()));
  const filtered = Boolean(query.trim() || scope !== "all");
  return <div className="kb-page kb-stack">
    <div className="kb-page-heading"><div><h1>知识库</h1><p className="kb-muted">让文档成为可检索、可追溯的业务知识。</p></div><Space wrap>
      <Button icon={<RefreshCw size={16} />} aria-label="刷新知识库列表" loading={loading} onClick={() => void refresh()}>刷新</Button>
      <Button type="primary" icon={<Plus size={16} />} onClick={() => setCreating(true)}>新建知识库</Button>
    </Space></div>
    <div className="kb-intro"><span className="kb-intro-icon"><LibraryBig size={28} /></span><div><strong>从资料到可靠的回答</strong><p>上传文档，等待索引发布，验证检索，再关联到 Agent。每条引用都保留文档版本与原文位置。</p><span className="kb-inline kb-muted"><ShieldCheck size={15} />默认私有 · 指定用户共享 · 访问逐次鉴权</span></div></div>
    <div className="kb-surface kb-stack">
      <div className="kb-toolbar"><div><strong>{data?.total ?? "—"} 个可访问知识库</strong><span className="kb-muted">{filtered ? ` · 本页匹配 ${items.length} 个` : " · 包含我的和共享给我的"}</span></div><div className="kb-toolbar-filters">
        <Input prefix={<Search size={16} />} aria-label="搜索本页知识库" placeholder="搜索本页知识库" allowClear value={query} onChange={(event) => setQuery(event.target.value)} />
        <Select aria-label="按本页权限筛选" value={scope} onChange={setScope} options={[{ value: "all", label: "全部权限" }, { value: "owned", label: "我创建的" }, { value: "shared", label: "共享给我" }]} />
      </div></div>
      {error ? <Alert type="error" showIcon message={error} action={<Button onClick={() => void refresh()}>重试</Button>} /> : loading ? <div className="kb-grid">{[1, 2, 3].map((key) => <Card key={key}><Skeleton active paragraph={{ rows: 3 }} /></Card>)}</div> : items.length ? <div className="kb-grid">{items.map((item) => <Card key={item.id} className="kb-resource-card">
        <div className="kb-card-heading"><span className="kb-resource-icon"><FolderOpen size={23} /></span><Tag>{item.owner_id === user.id ? "我的" : "共享 · 只读"}</Tag></div>
        <h2>{item.name}</h2><p className="kb-card-description">{item.description || "暂无用途说明"}</p>
        <div className="kb-inline"><Tag color={item.search_mode === "hybrid" ? "blue" : "default"}>{item.search_mode === "hybrid" ? "配置：混合检索" : "配置：关键词"}</Tag><span className="kb-muted">{item.active_index_id === null ? "尚无已发布索引" : `已发布 #${item.active_index_id}`}</span></div>
        <div className="kb-card-footer"><span className="kb-muted">配置 r{item.revision}</span><Button type="link" aria-label={`打开知识库 ${item.name}`} icon={<ArrowUpRight size={16} />} onClick={() => navigate(`/knowledge-bases/${item.id}`)}>打开知识库</Button></div>
      </Card>)}</div> : <div className="kb-empty"><Empty description={filtered ? "本页没有匹配结果，可清除筛选或切换分页" : "还没有可访问的知识库"}>{filtered ? <Button onClick={() => { setQuery(""); setScope("all"); }}>清除筛选</Button> : <Button type="primary" onClick={() => setCreating(true)}>创建第一个知识库</Button>}</Empty></div>}
      {data && data.total > 20 && <Pagination className="kb-pagination" current={page} pageSize={20} total={data.total} showSizeChanger={false} onChange={(value) => { setPage(value); setQuery(""); setScope("all"); }} />}
    </div>
    {creating && <KnowledgeEditor item={null} user={user} onClose={() => setCreating(false)} onSaved={(id) => { setCreating(false); navigate(`/knowledge-bases/${id}`); }} />}
  </div>;
}
