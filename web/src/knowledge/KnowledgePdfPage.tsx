import { useEffect, useState } from "react";
import { Alert, Button, Drawer, Spin } from "antd";
import { getKnowledgePdfPage } from "../api";
import type { KnowledgeCitation } from "../knowledge-api";

export default function KnowledgePdfPage({ citation }: { citation: KnowledgeCitation }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const source = citation.source_locator;
  useEffect(() => {
    if (!open) return;
    let active = true, objectUrl = "";
    const controller = new AbortController();
    setBusy(true); setError(""); setUrl("");
    getKnowledgePdfPage(citation.knowledge_base_id, citation.chunk_id, controller.signal).then(blob => {
      if (!active) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch((failure: unknown) => {
      if (active) setError(failure instanceof Error ? failure.message : "PDF 原页读取失败");
    }).finally(() => { if (active) setBusy(false); });
    const recheck = () => setRevision(value => value + 1);
    window.addEventListener("focus", recheck);
    return () => {
      active = false; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl);
      window.removeEventListener("focus", recheck);
    };
  }, [open, revision, citation.knowledge_base_id, citation.chunk_id]);
  const box = source?.bbox, width = source?.page_width, height = source?.page_height;
  return <>
    <Button size="small" onClick={() => setOpen(true)}>查看 PDF 原页 · 第 {citation.page} 页</Button>
    <Drawer open={open} title={`${citation.filename} · 第 ${citation.page} 页`} width="var(--yp-detail-width)" onClose={() => { setOpen(false); setUrl(""); setError(""); }}>
      <div className="kb-stack">
        <Alert type="info" showIcon message="高亮区域对应本条证据" description="原页用于核对 OCR、数值与版面关系。按当前权限读取原始版本，不重新加工或调用模型。" />
        {error && <Alert type="error" showIcon message={error} action={<Button onClick={() => setRevision(value => value + 1)}>重试</Button>} />}
        {busy && <Spin aria-label="正在读取 PDF 原页" />}
        {!busy && !error && url && <div className="kb-pdf-page">
          <img src={url} alt={`${citation.filename} 第 ${citation.page} 页原图`} />
          {box && width && height && <div className="kb-pdf-highlight" aria-label="证据所在区域" style={{
            left: `${box[0] / width * 100}%`, top: `${box[1] / height * 100}%`,
            width: `${(box[2] - box[0]) / width * 100}%`, height: `${(box[3] - box[1]) / height * 100}%`,
          }} />}
        </div>}
      </div>
    </Drawer>
  </>;
}
