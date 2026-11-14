import { Alert, Button, Checkbox, Spin, Tag, Tooltip, Upload, message } from "antd";
import { FilePlus2, Paperclip, RotateCcw, X } from "lucide-react";
import { uploadChatFile, type UploadedArtifact } from "../api";

import { createClientId } from "../browser-compat";
import { formatMiB, useRuntimeLimits } from "../runtime-limits";

export type ChatFile = { key: string; file: File; state: "uploading" | "ready" | "failed"; started?: boolean; artifact?: UploadedArtifact; context: boolean; error?: string };
const contextTypes = new Set(["text/plain", "text/markdown", "application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "image/png", "image/jpeg"]);

export default function ChatAttachments({ items, onChange, disabled }: {
  items: ChatFile[]; onChange: (update: (items: ChatFile[]) => ChatFile[]) => void; disabled: boolean;
}) {
  const { limits, error: limitsError, retry } = useRuntimeLimits();
  async function upload(item: ChatFile) {
    onChange(current => current.map(entry => entry.key === item.key ? { ...entry, state: "uploading", started: true, error: undefined } : entry));
    try {
      const artifact = await uploadChatFile(item.file);
      onChange(current => current.map(entry => entry.key === item.key ? { ...entry, state: "ready", artifact, context: contextTypes.has(artifact.media_type) } : entry));
    } catch (failure) {
      onChange(current => current.map(entry => entry.key === item.key ? { ...entry, state: "failed", error: failure instanceof Error ? failure.message : "上传失败" } : entry));
    }
  }
  return <div className="chat-attachments">
    {limitsError && <Alert type="error" showIcon message={`附件限制加载失败：${limitsError}`} action={<Button size="small" onClick={retry}>重试</Button>} />}
    <Upload.Dragger accept=".txt,.md,.pdf,.docx,.xlsx,.png,.jpg,.jpeg" multiple showUploadList={false} disabled={disabled || !limits}
      beforeUpload={(file, batch) => {
        if (!limits) return false;
        if (items.length + batch.indexOf(file) >= limits.max_files) { if (items.length + batch.indexOf(file) === limits.max_files) message.warning(`本轮最多附加 ${limits.max_files} 个文件`); return false; }
        if (file.size > limits.upload_max_bytes) { message.error(`每个文件最多 ${formatMiB(limits.upload_max_bytes)}`); return false; }
        const item: ChatFile = { key: createClientId(), file, state: "uploading", context: true };
        // 同一批多选使用函数更新核对总数，避免连续选择绕过五个文件上限。
        onChange(current => current.length < limits.max_files ? [...current, item] : current);
        return false;
      }}>
      <span className="chat-upload-label"><Paperclip size={16} /> 添加文件，或拖拽到这里 <small>{limits ? `最多 ${limits.max_files} 个 · 每个 ${formatMiB(limits.upload_max_bytes)}` : "正在加载上传限制…"}</small></span>
    </Upload.Dragger>
    <UploadQueue items={items} start={upload} />
    <div className="chat-file-list" aria-label="本轮附件">{items.map(item => <div className="chat-file" key={item.key}>
      <FilePlus2 size={17} aria-hidden="true" />
      <div className="chat-file-info"><Tooltip title={item.file.name}><strong>{item.file.name}</strong></Tooltip>
        <small>{Math.ceil(item.file.size / 1024)} KiB · {item.state === "uploading" ? "正在上传…" : item.state === "failed" ? item.error : "上传完成"}</small></div>
      {item.state === "uploading" ? <Spin size="small" /> : item.state === "failed" ? <Button type="text" aria-label={`重试上传 ${item.file.name}`} icon={<RotateCcw size={16} />} disabled={disabled} onClick={() => void upload(item)} /> :
        contextTypes.has(item.artifact?.media_type || "") ? <Checkbox checked={item.context} disabled={disabled} onChange={event => onChange(current => current.map(entry => entry.key === item.key ? { ...entry, context: event.target.checked } : entry))}>供模型读取</Checkbox> : <Tag>仅附加</Tag>}
      <Tooltip title="移除附件"><Button type="text" aria-label={`移除 ${item.file.name}`} icon={<X size={16} />} disabled={disabled} onClick={() => onChange(current => current.filter(entry => entry.key !== item.key))} /></Tooltip>
    </div>)}</div>
    {items.length > 0 && <p className="chat-file-notice">文本提取合计上限：{limits ? formatMiB(limits.context_max_bytes) : "加载中…"}；图片需要视觉模型。文件仅供本轮读取，长文档请拆分或导入知识库。</p>}
  </div>;
}

// 每个新增条目只启动一次；切换会话后的异步结果仍写回原会话草稿。
import { useEffect, useRef } from "react";
function UploadQueue({ items, start }: { items: ChatFile[]; start: (item: ChatFile) => Promise<void> }) {
  const started = useRef(new Set<string>());
  useEffect(() => {
    for (const item of items) {
      if (item.state === "uploading" && !item.started && !started.current.has(item.key)) {
        started.current.add(item.key); void start(item);
      }
    }
  }, [items, start]);
  return null;
}
