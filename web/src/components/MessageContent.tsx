import { memo, useMemo, useRef, useState, type ComponentPropsWithoutRef } from "react";
import { Button, Tooltip, message } from "antd";
import { Check, Copy } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import "../styles/message-content.css";
import RunKnowledgeEvidence from "../knowledge/RunKnowledgeEvidence";
import remarkLegacyCitation from "./remarkLegacyCitation";
import { copyText } from "../browser-compat";

export function CopyButton({ text, label = "复制消息" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copyRevision = useRef(0);
  async function copy() {
    const revision = ++copyRevision.current;
    try {
      await copyText(text);
      if (revision === copyRevision.current) setCopied(true);
    } catch { message.error("复制失败，请手动选择文本复制"); }
  }
  return <Tooltip title={copied ? "已复制" : (label === "复制消息" || label === "复制当前回复") ? "复制" : label} trigger={["hover", "focus"]}><Button type="text" size="small" className="message-copy" icon={copied ? <Check size={14} /> : <Copy size={14} />}
    aria-label={label} onClick={() => void copy()} onBlur={() => setCopied(false)} /></Tooltip>;
}

function CodeBlock({ children, ...props }: ComponentPropsWithoutRef<"pre">) {
  const ref = useRef<HTMLPreElement>(null);
  const language = (typeof children === "object" && children && "props" in children)
    ? String((children.props as { className?: string }).className || "").match(/language-([\w+-]+)/)?.[1] : undefined;
  const [copied, setCopied] = useState(false);
  async function copy() {
    try { await copyText(ref.current?.textContent || ""); setCopied(true); }
    catch { message.error("代码复制失败，请手动选择复制"); }
  }
  return <div className="message-code-block"><div className="message-code-header"><span>{language || "代码"}</span>
    <Tooltip title={copied ? "已复制" : "复制代码"} trigger={["hover", "focus"]}><Button type="text" size="small" className="message-copy" icon={copied ? <Check size={14} /> : <Copy size={14} />} aria-label="复制代码" onClick={() => void copy()} onBlur={() => setCopied(false)} /></Tooltip>
  </div><pre {...props} ref={ref}>{children}</pre></div>;
}

// Raw HTML stays escaped. Links are validated by react-markdown's default URL transform.
// Remote Markdown images require an explicit click instead of issuing background requests.
const components = {
  pre: ({ node: _node, ...props }: ComponentPropsWithoutRef<"pre"> & { node?: unknown }) => <CodeBlock {...props} />,
  a: ({ node: _node, ...props }: ComponentPropsWithoutRef<"a"> & { node?: unknown }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
  img: ({ src, alt }: ComponentPropsWithoutRef<"img">) => src ? <a href={src} target="_blank" rel="noopener noreferrer">图片：{alt || "查看图片"}</a> : <span>{alt || "图片地址不可用"}</span>,
  table: ({ node: _node, ...props }: ComponentPropsWithoutRef<"table"> & { node?: unknown }) => <div className="message-table-scroll" tabIndex={0} role="region" aria-label="消息表格"><table {...props} /></div>,
};

function formatSource(text: string, streaming: boolean): string {
  let language: string | undefined;
  if (/^\s*(?:<!doctype\s+html\b|<html(?:\s|>))/i.test(text)) language = "html";
  else if (!streaming && /^\s*[\[{]/.test(text)) {
    try { JSON.parse(text); language = "json"; } catch { /* Ordinary Markdown or incomplete JSON remains readable text. */ }
  }
  if (!language) return text;
  let fenceLength = 3;
  for (const match of text.matchAll(/`+/g)) fenceLength = Math.max(fenceLength, match[0].length + 1);
  const fence = "`".repeat(fenceLength);
  return `${fence}${language}\n${text}\n${fence}`;
}

export const MessageContent = memo(function MessageContent({ text, streaming = false, runId }: { text: string; streaming?: boolean; runId?: string }) {
  const messageComponents = useMemo(() => ({
    ...components,
    span: ({ node: _node, ...props }: ComponentPropsWithoutRef<"span"> & { node?: unknown }) => props.className === "message-citation-marker" && runId
      ? <RunKnowledgeEvidence runId={runId} legacyCitation={props.title} /> : <span {...props} />,
  }), [runId]);
  return <div className="message-markdown"><Markdown remarkPlugins={runId ? [remarkGfm, [remarkLegacyCitation, { streaming }]] : [remarkGfm]} rehypePlugins={streaming ? [] : [rehypeHighlight]} components={messageComponents}>{formatSource(text, streaming)}</Markdown></div>;
});
