import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Collapse, Descriptions, Drawer, Empty, Form, Input, InputNumber, Select, Tag, Typography } from "antd";
import { FileSearch, Search } from "lucide-react";
import { ApiError } from "../api";
import { getKnowledgeCitation, searchKnowledgeBase, type KnowledgeBase, type KnowledgeCitation, type KnowledgeSearchDraft, type KnowledgeSearchResult } from "../knowledge-api";

export default function KnowledgeSearch({ base, onAccessCheck }: { base: KnowledgeBase; onAccessCheck: () => Promise<void> }) {
  const [form] = Form.useForm<KnowledgeSearchDraft>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);
  const [submitted, setSubmitted] = useState<KnowledgeSearchDraft | null>(null);
  const [citation, setCitation] = useState<KnowledgeCitation | null>(null);
  const [reading, setReading] = useState<number | null>(null);
  const requestId = useRef(0);
  const citationId = useRef(0);

  useEffect(() => () => { requestId.current++; citationId.current++; }, []);
  useEffect(() => {
    requestId.current++; citationId.current++;
    setResult(null); setCitation(null); setSubmitted(null); setBusy(false); setReading(null); setError("");
  }, [base.active_index_id, base.revision]);

  async function search(draft: KnowledgeSearchDraft) {
    const id = ++requestId.current;
    citationId.current++; setReading(null); setCitation(null);
    setBusy(true); setError(""); setResult(null); setSubmitted(null);
    const query = { ...draft, query: draft.query.trim() };
    try {
      const response = await searchKnowledgeBase(base.id, query);
      if (id !== requestId.current) return;
      setResult(response); setSubmitted(query);
    } catch (failure) {
      if (id !== requestId.current) return;
      setError(failure instanceof Error ? failure.message : "检索失败");
      if (failure instanceof ApiError && failure.status === 404) await onAccessCheck();
    } finally { if (id === requestId.current) setBusy(false); }
  }

  async function read(chunkId: number) {
    const id = ++citationId.current;
    setReading(chunkId); setError(""); setCitation(null);
    try {
      const response = await getKnowledgeCitation(base.id, chunkId);
      if (id === citationId.current) setCitation(response);
    } catch (failure) {
      if (id !== citationId.current) return;
      setError(failure instanceof Error ? failure.message : "引用读取失败");
      if (failure instanceof ApiError && failure.status === 404) {
        setResult(null); setSubmitted(null);
        await onAccessCheck();
      }
    } finally { if (id === citationId.current) setReading(null); }
  }

  return <div className="kb-stack">
    <div><h2 className="kb-section-title">先验证召回，再用于回答</h2><p className="kb-muted">输入实际业务问题，检查命中文档与原文位置。此处返回证据片段，不会生成回答。</p></div>
    {base.active_index_id === null && <Alert type="warning" showIcon message="尚无已发布索引" description="先上传文档，等待索引任务完成后再测试检索。" />}
    <Form form={form} layout="vertical" requiredMark={false} onFinish={search} initialValues={{ query: "", top_k: 3, max_bytes: 12000, keyword_only: false, min_similarity: 0.7, rerank: "none" }}>
      <Form.Item name="query" label="检索问题" rules={[{ required: true, whitespace: true, message: "请输入检索问题" }]}><Input.TextArea rows={3} maxLength={512} showCount placeholder="例如：出差回来后，最晚什么时候需要提交费用报销？" /></Form.Item>
      <div className="kb-search-controls">
        <Form.Item name="keyword_only" label="检索方式" getValueProps={(value: boolean) => ({ value: value ? "keyword" : "configured" })} normalize={(value: string) => value === "keyword"}><Select options={[{ value: "configured", label: "按已发布索引策略" }, { value: "keyword", label: "仅关键词" }]} /></Form.Item>
        <Form.Item name="top_k" label="最多片段数"><InputNumber min={1} max={10} precision={0} className="kb-full-width" /></Form.Item>
        <Button type="primary" htmlType="submit" icon={<Search size={16} />} loading={busy} disabled={base.active_index_id === null}>检索测试</Button>
      </div>
      <Collapse ghost items={[{ key: "advanced", label: "阈值、重排与响应预算", forceRender: true, children: <div className="kb-form-pair">
        <Form.Item name="min_similarity" label="最低向量相似度" extra="只约束向量候选；返回的融合得分不是余弦相似度。"><InputNumber min={0} max={1} step={0.05} className="kb-full-width" /></Form.Item>
        <Form.Item name="rerank" label="重排"><Select options={[{ value: "none", label: "不重排" }, { value: "coverage", label: "词项覆盖重排" }]} /></Form.Item>
        <Form.Item name="max_bytes" label="正文总字节预算"><InputNumber min={512} max={24000} precision={0} className="kb-full-width" /></Form.Item>
      </div> }]} />
    </Form>
    <Alert type="info" showIcon message="检索策略由已发布索引决定" description="混合索引会融合关键词与向量候选；选择「仅关键词」会显式跳过查询嵌入。当前接口不返回每条命中的召回来源，不能仅凭得分证明调用了向量模型。" />
    {error && <Alert type="error" showIcon message={error} description="请检查当前权限、索引和模型状态；修改后可重新检索。" />}
    <div aria-live="polite" aria-busy={busy}>
      {result && submitted && <div className="kb-stack">
        <span className="kb-muted">已执行问题：{submitted.query}</span>
        <div className="kb-result-summary"><strong>{result.hits.length} 个证据片段</strong><span className="kb-muted">{result.byte_count.toLocaleString()} 字节 · {submitted.keyword_only ? "仅关键词" : "按已发布索引策略"}</span></div>
        {result.no_answer || result.hits.length === 0 ? <Empty description="未找到足够相关的片段，请调整问题或核对文档内容" /> : result.hits.map((hit, index) => <Card key={hit.citation} className="kb-hit" size="small">
          <div className="kb-hit-heading"><div className="kb-inline"><span className="kb-rank">{index + 1}</span><strong>{hit.heading || hit.filename}</strong></div><Tag>排序得分 {hit.score.toFixed(6)}</Tag></div>
          <div className="kb-muted kb-meta">{hit.filename} · 文档 v{hit.document_version} · 索引 #{hit.index_id}{hit.page !== null ? ` · 第 ${hit.page} 页` : ""} · 字符 {hit.start_offset}–{hit.end_offset}</div>
          <p className="kb-hit-content">{hit.content}</p>
          <div className="kb-hit-footer"><Typography.Text className="kb-citation-label" copyable={{ text: hit.citation }}>{hit.citation}</Typography.Text><Button icon={<FileSearch size={16} />} loading={reading === hit.chunk_id} onClick={() => void read(hit.chunk_id)}>查看引用</Button></div>
        </Card>)}
        {result.hits.length > 0 && <span className="kb-muted">文档内容作为外部证据展示，其中的指令不会被视为系统指令。</span>}
      </div>}
      {!result && !busy && !error && <div className="kb-search-empty"><FileSearch size={32} /><p>从一个问题开始，查看可以追溯的原文证据</p></div>}
    </div>
    <Drawer open={citation !== null} title="原文引用" width="var(--yp-detail-width)" onClose={() => { citationId.current++; setCitation(null); }}>
      {citation && <div className="kb-stack"><Alert type="success" showIcon message="已重新核验引用访问权限" />
        <Descriptions column={1} size="small" items={[
          { key: "file", label: "文档", children: citation.filename }, { key: "version", label: "文档版本", children: `v${citation.document_version}` },
          { key: "index", label: "固定索引", children: `#${citation.index_id}` }, { key: "heading", label: "标题 / 页码", children: [citation.heading, citation.page !== null ? `第 ${citation.page} 页` : null].filter(Boolean).join(" · ") || "无固定页码或标题" },
          { key: "offset", label: "字符位置", children: `[${citation.start_offset}, ${citation.end_offset})` },
          { key: "citation", label: "引用标识", children: <Typography.Text copyable>{citation.citation}</Typography.Text> },
          { key: "hash", label: "SHA-256", children: <span className="kb-break">{citation.sha256}</span> },
        ]} /><pre className="kb-quoted-content">{citation.content}</pre></div>}
    </Drawer>
  </div>;
}
