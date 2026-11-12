import { useLayoutEffect, useRef } from "react";
import { MessageContent } from "./MessageContent";
import { useStreamingText } from "./useStreamingText";

export default function StreamingMessage({ text, active = true, initialText = "", onProgress }: {
  text: string; runId: string; active?: boolean; initialText?: string; onProgress?: (visible: string) => void;
}) {
  const visible = useStreamingText(text, initialText);
  const callback = useRef(onProgress);
  callback.current = onProgress;
  const pending = active || visible !== text;
  useLayoutEffect(() => { callback.current?.(visible); }, [visible]);
  return <div className="message-streaming" aria-busy={pending}>
    <MessageContent text={visible} streaming={pending} />
    {pending && <span className="message-stream-cursor" aria-hidden="true" />}
  </div>;
}
