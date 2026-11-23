import { useEffect, useState } from "react";
import { Button } from "antd";
import { listRunArtifacts, type MessageArtifact } from "../api";
import MessageFiles from "./MessageFiles";

export default function RunToolResults({ runId, active }: { runId: string; active: boolean }) {
  const [files, setFiles] = useState<MessageArtifact[]>([]);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let current = true, inFlight = false;
    setFiles([]); setError("");
    async function load() {
      if (inFlight) return;
      inFlight = true;
      try {
        const items = await listRunArtifacts(runId);
        if (current) { setFiles(items.filter(item => item.tool_result).map(item => ({ ...item, artifact_id: item.id, available: true }))); setError(""); }
      } catch { if (current) { setFiles([]); setError("工具结果列表暂时无法加载"); } }
      finally { inFlight = false; }
    }
    void load();
    const timer = active ? window.setInterval(() => void load(), 3000) : null;
    return () => { current = false; if (timer) window.clearInterval(timer); };
  }, [runId, active, revision]);
  if (!files.length && !error) return null;
  return <div aria-label="本轮工具结果">{error && <Button type="text" onClick={() => setRevision(value => value + 1)}>{error}，重试</Button>}<MessageFiles files={files} generated /></div>;
}
