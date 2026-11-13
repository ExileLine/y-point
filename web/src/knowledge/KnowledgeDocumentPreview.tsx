import { useEffect, useRef, useState } from "react";
import { Alert, Button, Card, Drawer, Empty, Skeleton } from "antd";
import { ApiError } from "../api";
import { getKnowledgeProcessingPreview, type KnowledgeDocument, type KnowledgeProcessingPreview } from "../knowledge-api";
import KnowledgeEvidence from "./KnowledgeEvidence";

export default function KnowledgeDocumentPreview({ baseId, document, onClose }: { baseId: number; document: KnowledgeDocument; onClose: () => void }) {
  const [preview, setPreview] = useState<KnowledgeProcessingPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  const controller = useRef<AbortController>();

  async function load(append = false) {
    const current = ++generation.current;
    controller.current?.abort();
    controller.current = new AbortController();
    setBusy(true); setError("");
    if (!append) setPreview(null);
    try {
      let next = await getKnowledgeProcessingPreview(baseId, document.id, append ? preview?.next_after ?? -1 : -1, controller.current.signal);
      if (current !== generation.current) return;
      if (append && preview && preview.index_id !== next.index_id) {
        // 活动索引切代后从首页重新读取，旧游标不应用于新代。
        next = await getKnowledgeProcessingPreview(baseId, document.id, -1, controller.current.signal);
        if (current !== generation.current) return;
        append = false;
      }
      setPreview(previous => append && previous && previous.index_id === next.index_id
        ? { ...next, items: [...previous.items, ...next.items.filter(item => !previous.items.some(existing => existing.chunk_id === item.chunk_id))] } : next);
    } catch (failure) {
      if (current !== generation.current) return;
      if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) setPreview(null);
      setError(failure instanceof Error ? failure.message : "加工结果读取失败");
    } finally { if (current === generation.current) setBusy(false); }
  }

  useEffect(() => {
    void load();
    // 再次聚焦时重新鉴权，预览只读取已发布证据，不触发模型。
    const recheck = () => { void load(); };
    window.addEventListener("focus", recheck);
    return () => { generation.current++; controller.current?.abort(); window.removeEventListener("focus", recheck); };
  }, [baseId, document.id]);

  return <Drawer open title={`加工结果 · ${document.filename}`} width="var(--yp-detail-width)" onClose={onClose}>
    <div className="kb-stack">
      <Alert type="info" showIcon message="原始证据与模型解释分别展示" description="这里读取已发布索引，不重新解析或调用模型。数据区域可追溯到原始文件；新版本处理中仍展示旧代。" />
      {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => void load()}>重试</Button>} />}
      {busy && !preview && <Skeleton active />}
      {preview && <>
        {preview.processing_metadata?.pages && <Alert type="info" showIcon message={`PDF 共 ${preview.processing_metadata.pages.length} 页 · OCR ${preview.processing_metadata.ocr_pages || 0} 页 · 空白 ${preview.processing_metadata.blank_pages || 0} 页`}
          description={<div>{preview.processing_metadata.pages.map(page => <div key={page.page}>第 {page.page} 页 · {{ text: "文字页", scan: "扫描页", mixed: "混合页", blank: "空白页（跳过）" }[page.kind]}
            {page.method && ` · ${page.method === "ocr" ? "OCR" : "文字层"}`}{page.confidence != null && ` · 置信度 ${page.confidence.toFixed(1)}%`}</div>)}</div>} />}
        {preview.processing_metadata?.format && preview.processing_metadata.format !== "pdf" && <Alert type="info" showIcon
          message={`${preview.processing_metadata.format.toUpperCase()} · 已按文档结构提取 ${preview.processing_metadata.block_count || 0} 条证据`}
          description={<div>
            {preview.processing_metadata.format === "html" && <div>优先提取 main/article 主文，已排除 {preview.processing_metadata.excluded_regions || 0} 个噪声区域；不加载网页资源或执行脚本。</div>}
            {["json", "yaml"].includes(preview.processing_metadata.format) && <div>按配置路径和父对象组织字段，数组记录分开；值来自文件，不代表系统实际运行状态。</div>}
            {preview.processing_metadata.format === "pptx" && <div>共 {preview.processing_metadata.slide_count || 0} 页 · 排除隐藏页 {preview.processing_metadata.hidden_slides || 0} 页 · 备注证据 {preview.processing_metadata.notes_blocks || 0} 条</div>}
            {preview.processing_metadata.warnings?.map(warning => <div key={warning}>{warning}</div>)}
          </div>} />}
        {preview.indexed_version !== null && <span className="kb-muted">索引 #{preview.index_id} · 已索引文档 v{preview.indexed_version} · 当前上传 v{preview.current_version}</span>}
        {preview.indexed_version !== null && preview.indexed_version !== preview.current_version && <Alert type="warning" showIcon message="当前展示旧版本，新版本尚未完整发布" />}
        {!preview.items.length && <Empty description="此文档尚无已发布的证据，请先检查索引任务" />}
        {preview.items.map((item, index) => <Card key={item.citation} size="small" title={`证据 ${index + 1}`}><KnowledgeEvidence citation={item} /></Card>)}
        {preview.has_more && <Button loading={busy} onClick={() => void load(true)}>加载更多证据</Button>}
      </>}
    </div>
  </Drawer>;
}
