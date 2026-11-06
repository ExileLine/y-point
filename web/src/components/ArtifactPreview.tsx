import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Empty, Spin, Tabs, Tooltip } from "antd";
import { Download } from "lucide-react";
import { getArtifactPreview, type ArtifactPreview as Preview, type MessageArtifact } from "../api";
import { CopyButton, MessageContent } from "./MessageContent";
import "../styles/artifact-preview.css";

function columnName(index: number): string {
  let value = index + 1, result = "";
  while (value > 0) { value--; result = String.fromCharCode(65 + value % 26) + result; value = Math.floor(value / 26); }
  return result;
}

function Sheet({ rows }: { rows: string[][] }) {
  if (!rows.length) return <Empty description="工作表暂无内容" />;
  const columns = Math.max(...rows.map(row => row.length));
  return <div className="artifact-preview-table-scroll" tabIndex={0} role="region" aria-label="表格内容，可横向滚动">
    <table><thead><tr><th scope="col">行</th>{Array.from({ length: columns }, (_, index) => <th key={index} scope="col">{columnName(index)}</th>)}</tr></thead>
      <tbody>{rows.map((row, index) => <tr key={index}><th scope="row">{index + 1}</th>{Array.from({ length: columns }, (_, column) => <td key={column}>{row[column] || ""}</td>)}</tr>)}</tbody>
    </table>
  </div>;
}

function htmlDocument(content: string): string {
  // 内容仍置于无权限沙箱，CSP 禁止脚本、外部资源、表单及嵌套框架。
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'">
<style>body{margin:24px;color:#182230;font:16px/1.75 system-ui,sans-serif;overflow-wrap:anywhere}h1,h2,h3{line-height:1.4}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}table{border-collapse:collapse;display:block;max-width:100%;overflow:auto}td,th{border:1px solid #d0d5dd;padding:8px;white-space:pre-wrap}a{color:inherit}blockquote{border-left:3px solid #d0d5dd;margin:16px 0;padding-left:16px}</style></head><body>${content}</body></html>`;
}

export function PreviewContent({ preview }: { preview: Preview }) {
  if (preview.kind === "html") return <iframe className="artifact-preview-html" title="文档内容预览" sandbox="" referrerPolicy="no-referrer" srcDoc={htmlDocument(preview.content)} />;
  if (preview.kind === "sheets") return preview.sheets.length ? <Tabs className="artifact-preview-sheets" items={preview.sheets.map((sheet, index) => ({ key: String(index), label: sheet.name, children: <Sheet rows={sheet.rows} /> }))} /> : <Empty description="暂无表格内容" />;
  if (!preview.content) return <Empty description="文档暂无可预览内容" />;
  return <div className="artifact-preview-document">
    <div className="artifact-preview-copy"><CopyButton text={preview.content} label="复制文档内容" /></div>
    {preview.kind === "markdown" ? <MessageContent text={preview.content} /> : <pre className="artifact-preview-text">{preview.content}</pre>}
  </div>;
}

export default function ArtifactPreview({ file, onClose, onDownload, downloading }: {
  file: MessageArtifact; onClose: () => void; onDownload: () => void; downloading: boolean;
}) {
  const [preview, setPreview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setPreview(null); setError(""); setLoading(true);
    if (!file.available) { setError("文件已不可用，请关闭预览。"); setLoading(false); return; }
    void getArtifactPreview(file.artifact_id, controller.signal).then(result => {
      if (active) setPreview(result);
    }).catch(failure => {
      if (active) setError(failure instanceof Error ? failure.message : "预览加载失败");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, [file.artifact_id, file.available, revision]);
  return <Drawer open title="文件预览" width="min(100vw, 960px)" rootClassName="artifact-preview-drawer" onClose={onClose}
    extra={<Tooltip title="下载原件" trigger={["hover", "focus"]}><Button type="text" className="artifact-preview-download" aria-label="下载原件"
      icon={<Download size={18} />} loading={downloading} disabled={!file.available} onClick={onDownload} /></Tooltip>}>
    <div className="artifact-preview-heading"><strong>{preview?.filename || file.filename}</strong><span>只读内容预览</span></div>
    {loading ? <div className="artifact-preview-loading" role="status"><Spin /><span>正在加载预览…</span></div> : error ?
      <Alert type="error" showIcon message="无法预览文件" description={error} action={<Button size="small" onClick={() => setRevision(value => value + 1)}>重试</Button>} /> : preview && <>
        {preview.notice && <Alert type="info" showIcon message={preview.notice} />}
        {preview.truncated && <Alert type="warning" showIcon message="内容较长，仅展示部分预览；请下载原件查看完整内容。" />}
        <div className="artifact-preview-content"><PreviewContent preview={preview} /></div>
      </>}
  </Drawer>;
}
