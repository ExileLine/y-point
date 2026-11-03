import { useRef, useState } from "react";
import { Button, Tooltip, message } from "antd";
import { Download, FileText } from "lucide-react";
import { downloadArtifact, type MessageArtifact } from "../api";

function FileItem({ file, generated }: { file: MessageArtifact; generated: boolean }) {
  const [downloading, setDownloading] = useState(false);
  const inFlight = useRef(false);
  async function download() {
    if (inFlight.current || !file.available) return;
    inFlight.current = true;
    setDownloading(true);
    try {
      await downloadArtifact(file.artifact_id, file.filename);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "下载失败，请重试");
    } finally {
      inFlight.current = false;
      setDownloading(false);
    }
  }
  const label = file.available ? `下载 ${file.filename}` : "文件不可用，无法下载";
  return <div className={`session-file${file.available ? "" : " session-file-unavailable"}`}>
    <FileText size={18} aria-hidden="true" />
    <div className="session-file-info">
      <span className="session-file-name">{file.filename}</span>
      <small>{generated ? "模型生成" : "附件"}{!file.available ? " · 不可用" : file.included_in_context ? " · 已提供给模型" : ""}</small>
    </div>
    <Tooltip title={label} trigger={["hover", "focus"]}>
      <Button type="text" className="session-file-download" icon={<Download size={16} />} aria-label={label}
        disabled={!file.available} loading={downloading} onClick={() => void download()} />
    </Tooltip>
  </div>;
}

export default function MessageFiles({ files, generated = false }: { files: MessageArtifact[]; generated?: boolean }) {
  if (!files.length) return null;
  return <div className="session-message-files" role="group" aria-label={generated ? "生成产物" : "消息附件"}>
    {files.map(file => <FileItem key={file.artifact_id} file={file} generated={generated} />)}
  </div>;
}
