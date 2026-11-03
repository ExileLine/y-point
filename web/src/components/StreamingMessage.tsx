import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { MessageContent } from "./MessageContent";

// Animate only the presentation. The full SSE text and server sequence cursor stay authoritative.
export default function StreamingMessage({ text, runId, onProgress }: { text: string; runId: string; onProgress: () => void }) {
  const [visible, setVisible] = useState("");
  const target = useRef(text);
  const shown = useRef("");
  const progress = useRef(onProgress);
  target.current = text;
  progress.current = onProgress;

  useEffect(() => {
    let frame: number;
    let lastTime = 0;
    let active = true;
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const boundaries = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    shown.current = "";
    setVisible("");
    function tick(time: number) {
      if (!active) return;
      if (time - lastTime >= 32) {
        const elapsed = Math.min(time - (lastTime || time - 32), 64);
        lastTime = time;
        if (shown.current !== target.current) {
          const remaining = [...boundaries.segment(target.current.slice(shown.current.length))];
          // Small deltas appear gradually; large backlogs catch up without a long artificial delay.
          const count = preference.matches ? remaining.length : Math.max(1, Math.ceil(remaining.length * (1 - Math.exp(-elapsed / 100))));
          const next = remaining[count]?.index ?? target.current.length - shown.current.length;
          shown.current = target.current.slice(0, shown.current.length + next);
          setVisible(shown.current);
        }
      }
      frame = requestAnimationFrame(tick);
    }
    frame = requestAnimationFrame(tick);
    return () => { active = false; cancelAnimationFrame(frame); };
  }, [runId]);

  useLayoutEffect(() => { progress.current(); }, [visible]);
  return <div className="message-streaming" aria-busy="true"><MessageContent text={visible} streaming /><span className="message-stream-cursor" aria-hidden="true" /></div>;
}
