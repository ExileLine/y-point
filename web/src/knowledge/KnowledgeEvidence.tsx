import { Alert } from "antd";
import KnowledgePdfPage from "./KnowledgePdfPage";
import type { KnowledgeCitation } from "../knowledge-api";

export function KnowledgeSource({ citation }: { citation: KnowledgeCitation }) {
  const source = citation.source_locator;
  if (source?.kind === "html") return <div className="kb-source-location" aria-label="HTML 来源">
    <strong>HTML 主文{source.heading_path?.length ? ` · ${source.heading_path.join(" / ")}` : ""}</strong>
    <span>{source.role === "table" ? `表 ${source.table} · 第 ${source.row} 行` : source.role === "code" ? "代码文本" : source.role === "heading" ? "标题" : "正文"}
      {source.element_id ? ` · 元素 #${source.element_id}` : ""}{source.line ? ` · 源文件第 ${source.line} 行` : ""}</span>
    {source.merged_cells && <span>含合并单元格，保留位置列；字段关系请核对原文件。</span>}
  </div>;
  if (source?.kind === "config") return <div className="kb-source-location" aria-label="配置来源">
    <strong>{source.format?.toUpperCase()} 配置 · {source.pointer || "根对象"}</strong>
    <span>路径使用 JSON Pointer，字段类型和值独立保留；不执行配置、模板或变量替换。</span>
    {source.paths?.map(path => <span key={path}>字段路径：{path || "根值"}</span>)}
    {source.line_start != null && <span>值定义于源文件第 {source.line_start}{source.line_end !== source.line_start ? `–${source.line_end}` : ""} 行</span>}
  </div>;
  if (source?.kind === "slide") return <div className="kb-source-location" aria-label="幻灯片来源">
    <strong>PPTX · 第 {source.slide} 页{source.slide_title ? ` · ${source.slide_title}` : ""}</strong>
    <span>{source.role === "notes" ? "演讲者备注 · 与正文分开" : source.role === "table" ? `表 ${source.table} · 第 ${source.row} 行` : source.role === "heading" ? "幻灯片标题" : "幻灯片正文"}
      {source.shape_id ? ` · 对象 ${source.shape_id}` : ""}</span>
    <span>按文件对象顺序提取；图片、图表和复杂版面请核对原文件。</span>
    {source.merged_cells && <span>含合并单元格，不推测空白位置对应的值。</span>}
  </div>;
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
