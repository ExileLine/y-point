import { useEffect, useState } from "react";
import { Alert, Button, Spin } from "antd";
import { getToolResultPage, type MessageArtifact, type ToolResultPage } from "../api";

export default function ToolResultPreview({ file }: { file: MessageArtifact }) {
  const [page, setPage] = useState<ToolResultPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [previous, setPrevious] = useState<number[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setPage(null);
    if (!file.available || !file.sha256) { setError("结果引用缺少摘要或已不可用，请刷新文件列表。"); setLoading(false); return; }
    void getToolResultPage(file.artifact_id, file.sha256, offset, controller.signal).then(result => {
      if (!controller.signal.aborted) {
        if (result.sha256 !== file.sha256 || result.offset !== offset) throw new Error("结果页与当前快照不一致，请刷新后重试。");
        setPage(result);
      }
    }).catch(failure => { if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "读取结果失败"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [file.artifact_id, file.available, file.sha256, offset, revision]);
  return <div className="tool-result-preview" aria-label="MCP 结果分段预览">
    <Alert type="info" showIcon message="单次远端响应的脱敏快照" description="预览为 JSON 文本片段，远端可能另有分页。只读取部分时不能作全量结论；下载包含本次响应范围内的完整脱敏内容。" />
    {loading ? <Spin aria-label="正在读取结果" /> : error ? <Alert type="error" message={error} action={<Button onClick={() => setRevision(value => value + 1)}>重试读取</Button>} /> : page && <>
      <p>字符 {page.offset}–{page.end_offset} / {page.total_characters} · {page.complete ? "已到文件结尾" : "后续仍有内容"}</p>
      <pre className="artifact-preview-text tool-result-fragment">{page.text_fragment}</pre>
      <Button disabled={previous.length === 0} onClick={() => { setOffset(previous[previous.length - 1]); setPrevious(items => items.slice(0, -1)); }}>上一段</Button>
      <Button disabled={page.next_offset === null} onClick={() => { setPrevious(items => [...items, offset]); setOffset(page.next_offset!); }}>继续读取</Button>
    </>}
  </div>;
}
