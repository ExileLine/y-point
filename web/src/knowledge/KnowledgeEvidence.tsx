import { Alert } from "antd";
import KnowledgePdfPage from "./KnowledgePdfPage";
import type { KnowledgeCitation } from "../knowledge-api";

export function KnowledgeSource({ citation }: { citation: KnowledgeCitation }) {
  const source = citation.source_locator;
  if (source?.kind === "pdf") return <div className="kb-source-location" aria-label="PDF 来源">
    <strong>PDF · 第 {source.page} 页 · {source.extraction_method === "ocr" ? "OCR 识别" : "文字层提取"}</strong>
    <span>{source.layout === "table" ? "表格记录" : source.layout === "unverified" ? "识别行 · 布局关系待核对" : "正文区域"}
      {source.confidence != null && ` · OCR 置信度 ${source.confidence.toFixed(1)}%`}</span>
    {source.bbox && <span>区域坐标 [{source.bbox.map(value => Math.round(value)).join(", ")}] · 原点为页面左上角</span>}
    {source.review_required && <span>OCR 可能误识别；数值、单位和表格关系请核对原页，置信度不是事实准确率。</span>}
  </div>;
  if (!source || source.kind !== "table") return null;
  return <div className="kb-source-location" aria-label="表格来源">
    <strong>{source.format === "csv" ? "CSV 记录" : `工作表：${source.sheet}`}</strong>
    <span>数据区域 {source.range} · 表头 {source.header_range}</span>
    {source.columns && <span>引用列：{source.columns.join("、")}</span>}
    {source.hidden_excluded && <span>已排除隐藏行列，区域范围可能包含被跳过的位置</span>}
  </div>;
}

export default function KnowledgeEvidence({ citation }: { citation: KnowledgeCitation }) {
  return <div className="kb-stack-tight">
    <KnowledgeSource citation={citation} />
    {citation.source_locator?.kind === "pdf" && <KnowledgePdfPage citation={citation} />}
    <pre className="kb-quoted-content">{citation.content}</pre>
    {citation.knowledge_notes && <Alert className="kb-generated-notes" type="info" showIcon
      message="模型解释 · 辅助检索" description={<><p>{citation.knowledge_notes}</p><span>以上解释由模型生成，事实与数值请以上方原始证据为准。</span></>} />}
  </div>;
}
