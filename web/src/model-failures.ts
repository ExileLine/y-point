/** Safe model access diagnostics shared by live runs, history and the catalog. */
export const modelAccessFailures: Record<string, string> = {
  model_not_found: "当前供应商未找到该模型或接口（404）。请管理员核对模型 ID、接口地址、部署状态及 API Key 的模型授权，修复后在“模型与供应商”测试文本调用。重建 Agent 或会话无法解决此问题。",
  model_authentication: "模型供应商认证失败（401）。请管理员更新 API Key 或认证配置，再测试文本调用；重建 Agent 或会话无法解决此问题。",
  model_access_denied: "当前凭据无权调用该模型（403）。请管理员检查 API Key 的模型授权，修复后测试文本调用；重建 Agent 或会话无法解决此问题。",
};
