const API_ROOT = "/api/v1";
const ACCESS_KEY = "yp.access_token";
const REFRESH_KEY = "yp.refresh_token";

type Envelope<T> = { code: number; message: string; data: T };

export type User = {
  id: number;
  username: string | null;
  nickname: string | null;
  role: string;
};

export type Agent = {
  id: number;
  name: string;
  description: string | null;
  icon: string | null;
  system_prompt: string;
  welcome_message: string | null;
  default_model_id: number;
  temperature: string;
  max_output_tokens: number;
  model_timeout_seconds: number;
  tool_timeout_seconds: number;
  max_model_calls: number | null;
  max_tool_calls: number | null;
  max_total_tokens: number | null;
  max_estimated_cost: string | null;
  enabled: boolean;
  version: number;
  skill_ids: number[];
  tool_ids: number[];
  knowledge_base_ids: number[];
  update_time: string;
};

export type SharedAgent = Pick<Agent, "id" | "name" | "description" | "icon" | "welcome_message" | "enabled"> & {
  owner_id: number;
};

export type AgentPage = { items: Agent[]; total: number; page: number; page_size: number };
export type Model = {
  id: number;
  provider_id: number;
  display_name: string;
  external_id: string;
  capabilities: string[];
  verified_capabilities: string[];
  capabilities_verified: boolean;
  context_window: number | null;
  input_price: string | null;
  output_price: string | null;
  price_currency: string;
  is_default: boolean;
  status: number | null;
};

export type AgentModelOption = Pick<Model, "id" | "display_name" | "context_window">;
export type ProviderKind = {
  kind: string;
  display_name: string;
  required_fields: string[];
  optional_fields: string[];
  suggested_base_url: string | null;
};
export type Provider = {
  id: number;
  name: string;
  kind: string;
  base_url: string;
  has_api_key: boolean;
  has_custom_headers: boolean;
  api_version: string | null;
  region: string | null;
  timeout_seconds: number;
  remark: string | null;
  status: number | null;
};
export type ProviderDraft = {
  name: string;
  kind?: string;
  base_url?: string | null;
  api_key?: string;
  custom_headers?: Record<string, string> | null;
  api_version?: string | null;
  region?: string | null;
  timeout_seconds?: number;
  remark?: string | null;
  status?: number;
};
export type ModelDraft = {
  provider_id: number;
  external_id: string;
  display_name: string;
  capabilities: string[];
  context_window?: number | null;
  input_price?: string | null;
  output_price?: string | null;
  price_currency: string;
  status: number;
};
export type ProbeResult = { available: boolean; latency_ms: number; upstream_status_code: number | null; failure: string | null };
export type DiscoveryResult = ProbeResult & { models: { external_id: string; display_name: string; already_added: boolean }[] };
export type CapabilityTestResult = { capability: string; result: ProbeResult; verified_capabilities: string[] };

type Tokens = { access_token: string; refresh_token: string; expires_in: number };

export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: number) {
    super(message);
    this.name = "ApiError";
  }
}

export function hasSession(): boolean {
  return Boolean(sessionStorage.getItem(ACCESS_KEY) || sessionStorage.getItem(REFRESH_KEY));
}

export function clearSession(): void {
  sessionStorage.removeItem(ACCESS_KEY);
  sessionStorage.removeItem(REFRESH_KEY);
}

function expireSession(): void {
  clearSession();
  window.dispatchEvent(new Event("yp:session-expired"));
}

function saveTokens(tokens: Tokens): void {
  sessionStorage.setItem(ACCESS_KEY, tokens.access_token);
  sessionStorage.setItem(REFRESH_KEY, tokens.refresh_token);
}

async function readResponse<T>(response: Response): Promise<T> {
  let payload: Envelope<T> | undefined;
  try {
    payload = (await response.json()) as Envelope<T>;
  } catch {
    throw new ApiError("服务端返回了无法读取的响应", response.status);
  }
  if (!response.ok || payload.code >= 400) {
    throw new ApiError(payload.message || "请求失败", response.status, payload.code);
  }
  return payload.data;
}

let refreshing: Promise<boolean> | null = null;

async function refreshSession(): Promise<boolean> {
  const refreshToken = sessionStorage.getItem(REFRESH_KEY);
  if (!refreshToken) return false;
  if (!refreshing) {
    refreshing = fetch(`${API_ROOT}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    })
      .then(readResponse<Tokens>)
      .then((tokens) => {
        saveTokens(tokens);
        return true;
      })
      .catch(() => {
        clearSession();
        return false;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

export async function request<T>(path: string, init: RequestInit = {}, retry = true): Promise<T> {
  const accessToken = sessionStorage.getItem(ACCESS_KEY);
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("无法连接服务端", 0);
  }
  if (response.status === 401 && retry && (await refreshSession())) {
    return request<T>(path, init, false);
  }
  if (response.status === 401 && accessToken && path !== "/auth/login") {
    expireSession();
  }
  return readResponse<T>(response);
}

export async function login(identifier: string, password: string): Promise<User> {
  const tokens = await request<Tokens>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ identifier, password }),
  }, false);
  saveTokens(tokens);
  try {
    return await getMe();
  } catch (error) {
    clearSession();
    throw error;
  }
}

export function getMe(): Promise<User> {
  return request<User>("/auth/me");
}

export async function logout(): Promise<void> {
  try {
    await request<void>("/auth/logout", { method: "POST" }, false);
  } finally {
    clearSession();
  }
}

export function listAgents(page: number): Promise<AgentPage> {
  return request<AgentPage>(`/agents?page=${page}&page_size=20`);
}

export function listSharedAgents(): Promise<SharedAgent[]> {
  return request<SharedAgent[]>("/agents/shared");
}

export function getAgent(id: number): Promise<Agent> {
  return request<Agent>(`/agents/${id}`);
}

export function getSharedAgent(id: number): Promise<SharedAgent> {
  return request<SharedAgent>(`/agents/shared/${id}`);
}

export function listModels(): Promise<Model[]> {
  return request<Model[]>("/models");
}

export function listAgentModels(): Promise<AgentModelOption[]> {
  return request<AgentModelOption[]>("/models/available");
}

export const listProviderKinds = (): Promise<ProviderKind[]> => request("/providers/kinds");
export const listProviders = (): Promise<Provider[]> => request("/providers");
export const createProvider = (draft: ProviderDraft): Promise<Provider> => request("/providers", { method: "POST", body: JSON.stringify(draft) });
export const updateProvider = (id: number, draft: Partial<ProviderDraft>): Promise<Provider> => request(`/providers/${id}`, { method: "PUT", body: JSON.stringify(draft) });
export const deleteProvider = (id: number): Promise<void> => request(`/providers/${id}`, { method: "DELETE" });
export const testProvider = (id: number): Promise<ProbeResult> => request(`/providers/${id}/test`, { method: "POST" });
export const discoverModels = (id: number): Promise<DiscoveryResult> => request(`/providers/${id}/available-models`);
export const createModel = (draft: ModelDraft): Promise<Model> => request("/models", { method: "POST", body: JSON.stringify(draft) });
export const updateModel = (id: number, draft: Partial<ModelDraft>): Promise<Model> => request(`/models/${id}`, { method: "PUT", body: JSON.stringify(draft) });
export const deleteModel = (id: number): Promise<void> => request(`/models/${id}`, { method: "DELETE" });
export const setDefaultModel = (id: number): Promise<Model> => request(`/models/${id}/default`, { method: "POST" });
export const testModel = (id: number): Promise<ProbeResult> => request(`/models/${id}/test`, { method: "POST" });
export const testModelCapability = (id: number, capability: string): Promise<CapabilityTestResult> => request(`/models/${id}/capabilities/test`, { method: "POST", body: JSON.stringify({ capability }) });
export const importModels = (provider_id: number, models: { external_id: string; display_name: string; capabilities: string[] }[]): Promise<{ created: Model[]; skipped_existing: string[] }> => request("/models/import", { method: "POST", body: JSON.stringify({ provider_id, models }) });

export type AgentDraft = {
  name: string;
  description?: string | null;
  system_prompt: string;
  welcome_message?: string | null;
  default_model_id?: number;
  temperature?: string;
  max_output_tokens?: number;
  model_timeout_seconds?: number;
  tool_timeout_seconds?: number;
  max_model_calls?: number | null;
  max_tool_calls?: number | null;
  max_total_tokens?: number | null;
  max_estimated_cost?: string | null;
  enabled: boolean;
  skill_ids?: number[];
  tool_ids?: number[];
  knowledge_base_ids?: number[];
};

export type ToolEnvironment = {
  state: "not_required" | "pending" | "building" | "ready" | "failed";
  failure_code: string | null;
  message: string | null;
  locked_requirements: { name: string; version: string; sha256: string }[];
};
export type UserTool = {
  id: number; owner_id: number; name: string; model_name: string; description: string;
  kind: "script" | "mcp"; visibility: "private" | "public"; enabled: boolean; current_version: number;
  input_schema?: Record<string, unknown>; script_language?: "python" | "bash" | null;
  source_code?: string | null; mcp_url?: string | null; mcp_tool_name?: string | null;
  has_credentials?: boolean; share_credentials?: boolean;
  verified?: boolean; requirements?: string[]; environment?: ToolEnvironment;
};
export type ToolDraft = {
  name: string; description: string; kind: "script" | "mcp"; visibility: "private" | "public";
  input_schema?: Record<string, unknown> | null; script_language?: "python" | "bash" | null;
  source_code?: string | null; mcp_url?: string | null; mcp_tool_name?: string | null;
  mcp_bearer_token?: string; share_credentials?: boolean; requirements?: string[];
};
export type ToolCatalog = {
  builtin: { model_name: string; description: string; kind: "builtin"; visibility: "builtin" }[];
  user_tools: UserTool[];
};
export type DiscoveredTool = { name: string; description: string; input_schema: Record<string, unknown> };
export const listToolCatalog = (): Promise<ToolCatalog> => request("/tools/catalog");
export const getUserTool = (id: number): Promise<UserTool> => request(`/tools/${id}`);
export const createUserTool = (draft: ToolDraft): Promise<UserTool> => request("/tools", { method: "POST", body: JSON.stringify(draft) });
export const updateUserTool = (
  id: number,
  draft: { expected_version: number; enabled?: boolean } & Omit<Partial<ToolDraft>, "name" | "kind" | "script_language">,
): Promise<UserTool> =>
  request(`/tools/${id}`, { method: "PUT", body: JSON.stringify(draft) });
export const retryToolEnvironment = (id: number, expectedVersion: number): Promise<UserTool> =>
  request(`/tools/${id}/environment/retry`, { method: "POST", body: JSON.stringify({ expected_version: expectedVersion }) });
export const deleteUserTool = (id: number): Promise<void> => request(`/tools/${id}`, { method: "DELETE" });
export const testUserTool = (id: number, args: Record<string, unknown>): Promise<{ result: string }> =>
  request(`/tools/${id}/test`, { method: "POST", body: JSON.stringify({ arguments: args }) });
export const discoverMcpTools = (url: string, bearerToken?: string): Promise<DiscoveredTool[]> =>
  request("/tools/mcp/discover", { method: "POST", body: JSON.stringify({ url, bearer_token: bearerToken || null }) });

export type Skill = {
  id: number; owner_id: number; name: string; description: string; category: string | null;
  visibility: "private" | "public"; enabled: boolean; current_version: number;
  files?: Record<string, string>; manifest?: { path: string; bytes: number; sha256: string }[];
};
export const listSkills = (): Promise<Skill[]> => request("/skills");
export const getSkill = (id: number): Promise<Skill> => request(`/skills/${id}`);
export const createSkill = (draft: { files: Record<string, string>; category?: string | null; visibility: "private" | "public" }): Promise<Skill> =>
  request("/skills", { method: "POST", body: JSON.stringify(draft) });
export const updateSkill = (id: number, draft: { expected_version: number; files?: Record<string, string>; category?: string | null; visibility?: "private" | "public"; enabled?: boolean }): Promise<Skill> =>
  request(`/skills/${id}`, { method: "PUT", body: JSON.stringify(draft) });
export const deleteSkill = (id: number): Promise<void> => request(`/skills/${id}`, { method: "DELETE" });

async function fileRequest(path: string, init: RequestInit = {}, retry = true): Promise<Response> {
  const headers = new Headers(init.headers);
  const access = sessionStorage.getItem(ACCESS_KEY);
  if (access) headers.set("Authorization", `Bearer ${access}`);
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  } catch {
    throw new ApiError("无法连接服务端", 0);
  }
  if (response.status === 401 && retry && await refreshSession()) return fileRequest(path, init, false);
  if (response.status === 401 && access) expireSession();
  if (!response.ok) {
    await readResponse(response);
    throw new ApiError("文件操作失败", response.status);
  }
  return response;
}
export async function importSkill(file: File, visibility?: "private" | "public"): Promise<Skill> {
  const form = new FormData();
  form.append("file", file);
  return readResponse<Skill>(await fileRequest(`/skills/import${visibility ? `?visibility=${visibility}` : ""}`, { method: "POST", body: form }));
}
export async function downloadSkill(id?: number): Promise<void> {
  const response = await fileRequest(id ? `/skills/${id}/export` : "/skills/template");
  await saveFileResponse(response, id ? `skill-${id}.zip` : "skill-template.zip");
}

function downloadFilename(response: Response, fallback: string): string {
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = /filename\*=utf-8''([^;]+)/i.exec(disposition);
  const plain = /filename=(?:"([^"]*)"|([^;]+))/i.exec(disposition);
  let filename = plain?.[1] || plain?.[2]?.trim() || fallback;
  if (encoded) {
    try { filename = decodeURIComponent(encoded[1].trim()); } catch { /* Use the plain name or message filename. */ }
  }
  return filename.split(/[\\/]/).pop()?.replace(/[\u0000-\u001f\u007f]/g, "").trim() || "download";
}

async function saveFileResponse(response: Response, fallback: string): Promise<void> {
  // Blob preserves binary data and MIME; even HTML is saved rather than opened in the app.
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = downloadFilename(response, fallback);
  document.body.append(anchor);
  try { anchor.click(); } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
}

export async function downloadArtifact(artifactId: string, filename: string): Promise<void> {
  const response = await fileRequest(`/artifacts/${encodeURIComponent(artifactId)}/download`);
  await saveFileResponse(response, filename);
}

export type AgentShare = { user_id: number; username: string };
export const listAgentShares = (agentId: number): Promise<AgentShare[]> => request(`/agents/${agentId}/shares`);
export const searchShareTargets = (query: string): Promise<AgentShare[]> => request(`/agents/share-targets?query=${encodeURIComponent(query)}`);
export const grantAgentShare = (agentId: number, userId: number): Promise<AgentShare> => request(`/agents/${agentId}/shares`, { method: "POST", body: JSON.stringify({ user_id: userId }) });
export const revokeAgentShare = (agentId: number, userId: number): Promise<void> => request(`/agents/${agentId}/shares/${userId}`, { method: "DELETE" });
export const deleteAgent = (agentId: number): Promise<void> => request(`/agents/${agentId}`, { method: "DELETE" });

export type AgentSession = {
  id: string;
  agent_id: number;
  thread_id: string;
  title: string;
  archived: boolean;
  version: number;
  create_time: string;
  update_time: string;
};
export type AgentSessionPage = { items: AgentSession[]; total: number; page: number; page_size: number };
export type ArtifactPreview = {
  artifact_id: string;
  filename: string;
  media_type: string;
  kind: "text" | "markdown" | "html" | "sheets";
  content: string;
  sheets: { name: string; rows: string[][] }[];
  truncated: boolean;
  notice: string | null;
};
export const getArtifactPreview = (artifactId: string, signal?: AbortSignal): Promise<ArtifactPreview> =>
  request(`/artifacts/${encodeURIComponent(artifactId)}/preview`, { signal, cache: "no-store" });

export type MessageArtifact = { artifact_id: string; filename: string; available: boolean; included_in_context?: boolean };
export type RunCall = {
  call_id: string;
  kind: "skill" | "tool" | "knowledge";
  name: string;
  status: "running" | "succeeded" | "failed" | "interrupted";
  sequence: number;
};
export type SessionMessage = {
  id: number;
  run_id: string;
  role: "user" | "assistant";
  content: string;
  create_time: string;
  attachments: MessageArtifact[];
  artifacts: MessageArtifact[];
  calls?: RunCall[];
};
export type SessionRun = { id: string; session_id: string; agent_id: number; state: string; failure_code: string | null; create_time: string; update_time: string };
export type RunStreamEvent = { sequence: number; event_type: string; state: string | null; content: string | null };
export type RunSnapshot = { run: SessionRun; events: RunStreamEvent[]; latest_sequence: number; next_after: number; has_more: boolean };
export type SessionRunPage = { items: SessionRun[]; total: number; page: number; page_size: number };
export type SessionRunDraft = { input_text: string; request_key: string; publish_answer_artifact: boolean; artifact_ids: string[]; context_artifact_ids: string[] };

function sessionPath(agentId: number, sessionId?: string): string {
  return `/agents/${agentId}/sessions${sessionId ? `/${encodeURIComponent(sessionId)}` : ""}`;
}

export function listAgentSessions(agentId: number, page: number, archived: boolean, search: string): Promise<AgentSessionPage> {
  const query = new URLSearchParams({ page: String(page), page_size: "20", archived: String(archived) });
  if (search.trim()) query.set("search", search.trim());
  return request(`${sessionPath(agentId)}?${query}`);
}
export const createAgentSession = (agentId: number, title: string): Promise<AgentSession> => request(sessionPath(agentId), { method: "POST", body: JSON.stringify({ title }) });
export const getAgentSession = (agentId: number, sessionId: string): Promise<AgentSession> => request(sessionPath(agentId, sessionId));
export const updateAgentSession = (agentId: number, session: AgentSession, changes: { title?: string; archived?: boolean }): Promise<AgentSession> => request(sessionPath(agentId, session.id), { method: "PUT", body: JSON.stringify({ ...changes, expected_version: session.version }) });
export const deleteAgentSession = (agentId: number, sessionId: string): Promise<void> => request(sessionPath(agentId, sessionId), { method: "DELETE" });
export const listSessionMessages = (agentId: number, sessionId: string, after: number, limit = 100): Promise<SessionMessage[]> => request(`${sessionPath(agentId, sessionId)}/messages?after=${after}&limit=${limit}`);
export const listSessionRuns = (agentId: number, sessionId: string): Promise<SessionRunPage> => request(`${sessionPath(agentId, sessionId)}/runs?page=1&page_size=20`);
export const submitSessionRun = (agentId: number, sessionId: string, draft: SessionRunDraft): Promise<SessionRun> => request(`${sessionPath(agentId, sessionId)}/runs`, { method: "POST", body: JSON.stringify(draft) });
export const getRunCalls = (runId: string, signal?: AbortSignal): Promise<RunCall[]> => request(`/runs/${encodeURIComponent(runId)}/calls`, { signal });
export const getRun = (runId: string): Promise<SessionRun> => request(`/runs/${encodeURIComponent(runId)}`);
export const cancelRun = (runId: string): Promise<SessionRun> => request(`/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
export const issueRunStreamTicket = (runId: string): Promise<{ expires_in: number }> => request(`/runs/${encodeURIComponent(runId)}/stream-ticket`, { method: "POST" });
export const getRunSnapshot = (runId: string, after: number): Promise<RunSnapshot> => request(`/runs/${encodeURIComponent(runId)}/snapshot?after=${after}&limit=100`);
export const runStreamUrl = (runId: string, after: number): string => `${API_ROOT}/runs/${encodeURIComponent(runId)}/stream?after=${after}`;

export function createAgent(draft: AgentDraft & { default_model_id: number }): Promise<Agent> {
  return request<Agent>("/agents", { method: "POST", body: JSON.stringify(draft) });
}

export function updateAgent(id: number, version: number, draft: Partial<AgentDraft>): Promise<Agent> {
  return request<Agent>(`/agents/${id}`, {
    method: "PUT",
    body: JSON.stringify({ ...draft, expected_version: version }),
  });
}
