import { useEffect, useState } from "react";
import { request } from "./api";

export type RuntimeLimits = {
  upload_max_bytes: number;
  context_max_bytes: number;
  max_files: number;
  recursion_limit: number;
  mcp_connect_timeout_seconds: number;
  mcp_discovery_timeout_seconds: number;
  mcp_call_timeout_seconds: number;
  knowledge_search_timeout_seconds: number;
  knowledge_embedding_timeout_seconds: number;
  script_execution_timeout_seconds: number;
  script_test_timeout_seconds: number;
  default_model_timeout_seconds: number;
  max_model_timeout_seconds: number;
  default_tool_timeout_seconds: number;
  max_tool_timeout_seconds: number;
  default_max_output_tokens: number;
  max_output_tokens: number;
};

export function useRuntimeLimits(active = true) {
  const [limits, setLimits] = useState<RuntimeLimits | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!active) return;
    let current = true;
    setError("");
    setLimits(null);
    request<RuntimeLimits>("/agents/runtime-limits").then(value => {
      if (current) setLimits(value);
    }).catch((failure: unknown) => {
      if (current) setError(failure instanceof Error ? failure.message : "运行配置加载失败");
    });
    return () => { current = false; };
  }, [active, revision]);
  return { limits, error, retry: () => setRevision(value => value + 1) };
}

export function formatMiB(bytes: number) {
  return `${Number((bytes / (1024 * 1024)).toFixed(4))} MiB`;
}
