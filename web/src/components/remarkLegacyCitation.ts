// 只转换普通 Markdown 文本中的历史来源尾注，不修改代码、链接或原始消息。
type MarkdownNode = { type: string; value?: string; children?: MarkdownNode[] };

export default function remarkLegacyCitation({ streaming = false }: { streaming?: boolean } = {}) {
  return (tree: MarkdownNode) => {
    const walk = (node: MarkdownNode) => {
      if (!node.children || ["code", "inlineCode", "link", "linkReference", "html"].includes(node.type)) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== "text" || !child.value) { walk(child); return [child]; }
        const match = /(^|[。！？.!?；;\n]\s*)citation\s*[:：]\s*(\[\d+\](?:\s*[,，、]?\s*\[\d+\])*)\s*$/i.exec(child.value);
        if (match) {
          const before = child.value.slice(0, match.index) + match[1];
          const marker = {
            type: "legacyCitation",
            data: { hName: "span", hProperties: { className: "message-citation-marker", title: match[2] } },
            children: [{ type: "text", value: "来源说明" }],
          };
          return [...(before ? [{ type: "text", value: before }] : []), marker];
        }
        // 流式输出暂不展示尚未完成的历史尾注，完成后再生成来源入口。
        if (streaming) {
          const pending = /(^|[。！？.!?；;\n]\s*)citation\s*[:：]\s*\[[\d\s,，、]*$/i.exec(child.value);
          if (pending) return [{ ...child, value: child.value.slice(0, pending.index) + pending[1] }];
        }
        return [child];
      });
    };
    walk(tree);
  };
}
