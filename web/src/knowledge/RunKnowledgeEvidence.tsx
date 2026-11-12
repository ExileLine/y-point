import { useEffect, useRef, useState } from "react";
import { Alert, Button, Drawer, Empty, List, Space, Typography } from "antd";
import { FileSearch } from "lucide-react";
import { getKnowledgeCitation, listRunKnowledgeRetrievals, type KnowledgeCitation, type KnowledgeReference, type KnowledgeRetrieval } from "../knowledge-api";
import "../styles/knowledge.css";
import KnowledgeEvidence from "./KnowledgeEvidence";

export default function RunKnowledgeEvidence({ runId, legacyCitation }: { runId: string; legacyCitation?: string }) {
  const [open, setOpen] = useState(false);
  const [rows, setRows] = useState<KnowledgeRetrieval[]>([]);
  const [loading, setLoading] = useState(false);
  const [reading, setReading] = useState(false);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [citation, setCitation] = useState<KnowledgeCitation | null>(null);
  const sequence = useRef(0);
  const citationSequence = useRef(0);
  useEffect(() => () => { sequence.current++; citationSequence.current++; }, []);

  async function load(append = false) {
    const seq = ++sequence.current;
    if (!append) { setOpen(true); setRows([]); setCitation(null); citationSequence.current++; }
    setLoading(true); setError("");
    try {
      const response = await listRunKnowledgeRetrievals(runId, append ? rows.at(-1)?.id || 0 : 0);
      if (seq !== sequence.current) return;
      setRows((current) => append ? [...current, ...response.filter((row) => !current.some((item) => item.id === row.id))] : response);
      setMore(response.length === 50);
    } catch (failure) {
      if (seq === sequence.current) { setRows([]); setCitation(null); setMore(false); setError(failure instanceof Error ? failure.message : "知识检索记录读取失败"); }
    } finally { if (seq === sequence.current) setLoading(false); }
  }

  async function read(reference: KnowledgeReference, quiet = false) {
    const seq = ++citationSequence.current;
    if (!quiet) { setCitation(null); setReading(true); setError(""); }
    try {
      const response = await getKnowledgeCitation(reference.knowledge_base_id, reference.chunk_id);
      if (seq === citationSequence.current) setCitation(response);
    } catch (failure) {
      if (seq === citationSequence.current) { setCitation(null); setError(failure instanceof Error ? failure.message : "原文引用不可用"); }
    } finally { if (seq === citationSequence.current) setReading(false); }
  }

  useEffect(() => {
    if (!open || !citation) return;
    const check = () => { if (document.visibilityState === "visible") void read(citation, true); };
    const timer = window.setInterval(check, 15000);
    window.addEventListener("focus", check);
    return () => { window.clearInterval(timer); window.removeEventListener("focus", check); };
  }, [open, citation?.citation]);

  return <>
    <Button type="link" size="small" className={legacyCitation ? "message-citation-source" : undefined} title={legacyCitation ? `历史来源编号 ${legacyCitation}，点击查看说明及本轮检索记录` : undefined} icon={<FileSearch size={14} />} onClick={() => void load()}>{legacyCitation ? "来源说明" : "本轮知识检索记录"}</Button>
    <Drawer title="本轮知识检索记录" open={open} width="var(--yp-detail-width)" onClose={() => { sequence.current++; citationSequence.current++; setOpen(false); setCitation(null); setRows([]); setLoading(false); setReading(false); }}>
      <div className="kb-stack">
        <Alert type="info" showIcon message={legacyCitation ? "历史引用编号未与原文绑定" : "这里展示本轮知识检索返回的片段记录"}
          description={legacyCitation ? `回答中的 ${legacyCitation} 是模型生成的编号，尚未绑定具体原文。下方为本轮实际检索记录，点击片段可核对原文；本轮没有记录时，请回看前文。` : "记录不含查询正文。检索命中不代表最终回答一定采用了该片段；点击引用会按当前权限读取原文。"} />
        {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void load()}>重试</Button>} />}
        <List loading={loading || reading} dataSource={rows} locale={{ emptyText: error ? "记录暂不可用" : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="本轮没有知识检索记录" /> }} renderItem={(row) => <List.Item><div className="kb-stack-tight kb-full-width">
          <div className="kb-inline"><strong>检索 #{row.id}</strong><span className="kb-muted">{row.hit_count === 0 ? "未找到相关内容" : `${row.hit_count} 个片段 · ${row.byte_count} 字节`} · {Math.round(row.latency_ms)} ms</span></div>
          {row.embedding_usage.length > 0 && <span className="kb-muted">查询嵌入：{row.embedding_usage.map((usage) => `模型 #${usage.model_id} · ${usage.dimensions} 维 · ${usage.tokens} Token`).join("；")}</span>}
          {row.references.length > 0 && <Space wrap>{row.references.map((ref) => <Button key={`${ref.knowledge_base_id}:${ref.index_id}:${ref.chunk_id}`} size="small" disabled={reading} onClick={() => void read(ref)}>kb:{ref.knowledge_base_id}:i:{ref.index_id}:c:{ref.chunk_id}</Button>)}</Space>}
        </div></List.Item>} />
        {more && <Button loading={loading} onClick={() => void load(true)}>加载更多检索记录</Button>}
        {citation && <div className="kb-stack"><h3 className="kb-section-title">{citation.filename} · v{citation.document_version}</h3><Typography.Text copyable>{citation.citation}</Typography.Text><span className="kb-muted">索引 #{citation.index_id} · 字符 [{citation.start_offset}, {citation.end_offset}){citation.heading ? ` · ${citation.heading}` : ""}{citation.page !== null ? ` · 第 ${citation.page} 页` : ""}</span><KnowledgeEvidence citation={citation} /></div>}
      </div>
    </Drawer>
  </>;
}
