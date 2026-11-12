import { useId, useLayoutEffect, useRef, useState } from "react";
import { Brain, ChevronDown } from "lucide-react";
import { useStreamingText } from "./useStreamingText";

export default function ReasoningPanel({ text, active = false, onProgress }: { text: string; active?: boolean; onProgress?: () => void }) {
  const [choice, setChoice] = useState<boolean | null>(null);
  const expanded = choice ?? active;
  const id = useId();
  const content = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const visible = useStreamingText(text, active ? "" : text);
  useLayoutEffect(() => {
    if (content.current && expanded && following.current) content.current.scrollTop = content.current.scrollHeight;
    onProgress?.();
  }, [visible, expanded, onProgress]);
  if (!text) return null;
  return <section className={`reasoning-panel${active ? " reasoning-panel-active" : ""}`} aria-label="思考过程" aria-busy={active}>
    <button type="button" className="reasoning-toggle" aria-expanded={expanded} aria-controls={id} onClick={() => setChoice(!expanded)}>
      <Brain size={15} aria-hidden="true" /><span>{active ? "正在思考" : "思考过程"}</span>
      <span className="reasoning-toggle-hint">{expanded ? "收起" : "展开"}</span><ChevronDown size={14} aria-hidden="true" />
    </button>
    <div id={id} ref={content} className="reasoning-content" hidden={!expanded} role="region" aria-label="思考内容" tabIndex={expanded ? 0 : -1} onScroll={event => {
      const node = event.currentTarget;
      following.current = node.scrollHeight - node.scrollTop - node.clientHeight < 40;
    }}>{expanded ? visible : ""}</div>
  </section>;
}
