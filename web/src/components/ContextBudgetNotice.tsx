import { Alert, Descriptions } from "antd";
import type { ContextBudget } from "../api";

const names: Record<string, string> = { system: "系统提示", tools: "工具 Schema", history: "历史及工具结果", current_input: "当前输入", attachments: "参考附件", images: "图片预留" };
export default function ContextBudgetNotice({ budget }: { budget?: ContextBudget | null }) {
  if (!budget) return null;
  return <details className="context-budget-notice"><summary>上下文估算 · 输入 {budget.estimated_input_tokens.toLocaleString()} Token{budget.fits === false ? " · 超出预算" : budget.fits === null ? " · 窗口未配置" : ""}</summary>
    <Alert type={budget.fits === false ? "warning" : "info"} showIcon message={budget.notice} />
    <Descriptions size="small" column={1} items={[
      { key: "total", label: budget.scope === "current_input_lower_bound" ? "当前输入下限" : "完整请求", children: `${budget.serialized_input_bytes.toLocaleString()} 字节 · 输入上限 ${budget.input_limit_tokens?.toLocaleString() ?? "未知"} Token · 输出预留 ${budget.output_reserve_tokens?.toLocaleString() ?? "未知"} Token` },
      ...(budget.tool_loading ? [{ key: "loading", label: "MCP 工具加载", children: `按需加载 · 本轮固定 ${budget.tool_loading.available_mcp_tools} 个 · 本次绑定 ${budget.tool_loading.loaded_mcp_tools} 个` }] : []),
      ...Object.entries(budget.components).map(([key, value]) => ({ key, label: names[key] || key, children: `约 ${value.tokens.toLocaleString()} Token · ${value.bytes.toLocaleString()} 字节` })),
    ]} />
    {budget.suggestions.length > 0 && <p>{budget.suggestions.join("；")}</p>}
  </details>;
}
