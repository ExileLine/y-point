import { useEffect, useRef, useState } from "react";

// 原始事件和游标由页面维护；此处只调节可见字符，不延迟网络消费。
export function useStreamingText(text: string, initialText = "") {
  const [visible, setVisible] = useState(() => text.startsWith(initialText) ? initialText : text);
  const shown = useRef(visible);
  const target = useRef(text);
  const frame = useRef<number | null>(null);
  const preference = useRef<MediaQueryList | null>(null);
  target.current = text;

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    preference.current = media;
    const updatePreference = () => {
      if (media.matches) { shown.current = target.current; setVisible(target.current); }
    };
    media.addEventListener("change", updatePreference);
    return () => { media.removeEventListener("change", updatePreference); if (frame.current !== null) cancelAnimationFrame(frame.current); frame.current = null; };
  }, []);

  useEffect(() => {
    if (!text.startsWith(shown.current) || preference.current?.matches || document.hidden) {
      shown.current = text;
      setVisible(text);
    }
    if (shown.current === text || frame.current !== null) return;
    let previous = performance.now();
    let credit = 0;
    const segmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
    function tick(now: number) {
      frame.current = null;
      const rest = target.current.slice(shown.current.length);
      if (!rest) return;
      const elapsed = Math.min(64, now - previous);
      previous = now;
      // 小片段保持匀速，大批数据有界追平；仅分割本帧窗口，避免每帧扫描整篇长文。
      const speed = Math.max(90, rest.length / 0.45);
      credit += elapsed * speed / 1000;
      if (credit >= 1 || preference.current?.matches || document.hidden) {
        const wanted = preference.current?.matches || document.hidden ? rest.length : Math.min(rest.length, Math.floor(credit));
        let length = 0;
        for (const part of segmenter.segment(rest)) {
          if (part.index >= wanted) break;
          length = part.index + part.segment.length;
        }
        shown.current = target.current.slice(0, shown.current.length + length);
        credit = Math.max(0, credit - length);
        setVisible(shown.current);
      }
      if (shown.current !== target.current) frame.current = requestAnimationFrame(tick);
    }
    frame.current = requestAnimationFrame(tick);
  }, [text]);
  return visible;
}
