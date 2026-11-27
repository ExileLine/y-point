# 认证、授权与模型配置

创建首位管理员，管理登录会话、模型供应商、能力验证和 Agent 共享。

[文档总览](README.md) · [项目首页](../README.md)

## 认证与模型配置

首次部署先迁移数据库，再创建首位管理员。命令交互读取密码；密码至少 12 位，包含大小写字母和数字，UTF-8 编码不超过 72 字节。已有有效管理员时不能重复初始化。

```bash
uv run python -m app.cli.create_admin admin --mail admin@example.com
```

`POST /api/v1/auth/login` 使用用户名或邮箱及密码，返回访问令牌和轮换式刷新令牌。访问令牌有效期 7 天，刷新令牌有效期 30 天；服务端仅保存刷新令牌的 SHA-256 摘要。`POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout`、`GET/PUT /api/v1/auth/me`、`POST /api/v1/auth/password` 管理会话与个人资料。每次 Bearer 请求都会核对用户状态、令牌版本和登录会话。普通用户只能访问自己的资源或被指定共享的 Agent；管理员可通过 `/api/v1/users` 管理用户，并读取 `/api/v1/users/audit/events`。成功响应统一包含 `code`、`message` 和可选的 `data`，业务错误保留相应 HTTP 状态与错误码。

管理员通过 `/api/v1/providers` 管理供应商连接，通过 `/api/v1/models` 管理模型。`GET /api/v1/providers/kinds` 返回各类型的配置字段；支持 `openai_compatible`、`openai`、`anthropic`、`ollama`、`azure_openai`、`gemini` 和 `bedrock`。Ollama 可使用 `http://127.0.0.1:11434/v1`。模型记录外部 ID、能力、上下文窗口以及每百万 Token 的输入/输出价格；人工填写或导入的能力不等于已验证能力。

模型录入通常依次调用：

1. `POST /api/v1/providers` 创建连接；`POST /api/v1/providers/{provider_id}/test` 检查发现能力，`GET /api/v1/providers/{provider_id}/available-models` 读取候选模型。供应商不提供模型列表时可手动录入外部 ID。
2. `POST /api/v1/models/import` 批量导入，或 `POST /api/v1/models` 单独创建；导入请求最多 100 个模型。
3. `POST /api/v1/models/{model_id}/test` 测试最小文本调用；`POST /api/v1/models/{model_id}/capabilities/test` 逐项验证 `text`、`streaming`、`tools`、`structured_output`、`vision` 或 `reasoning_summary`。探测可能产生上游费用。页面也可通过「验证能力 → 一键验证已声明能力」确认后依次调用现有接口，展示逐项结果并支持停止后续验证；嵌入能力使用独立的 `embedding-test` 接口，详见[模型与供应商页面](web-model-catalog-page.md#一键验证已声明能力)。

能力验证每项最多请求 1024 个输出 Token，为推理与最终答案共用的输出预算留出空间；最小文本连通测试仍使用较小预算。上游明确返回 `length`、`max_tokens` 等截断终态，或 Responses 返回 `incomplete` 且原因为 `max_output_tokens` 时，验证结果为 `output_limit`（输出预算耗尽、暂无法判定），保留已有的该项验证，不把未验证能力标为通过，也不因预算耗尽自动追加请求。其他失败继续按原有单项规则更新验证结果。验证调用的实际费用取决于上游生成用量。

`reasoning_summary` 专指 OpenAI Responses API 的公开推理摘要，当前仅 `openai` 类型连接提供此项验证。OpenAI 兼容模型返回的 `reasoning_content` 或 `reasoning` 是公开思考文本，不能据此标记 Responses 摘要能力通过。某项摘要验证未通过不会清除已验证的文本能力；Agent 模型选择仍按所需能力判断。

OpenAI、OpenAI 兼容与 Ollama 的共享聊天适配器支持标准 JSON，以及兼容网关在非流式调用中返回的单条完整 `data: {JSON}` 响应（可带末尾 `[DONE]`）。正文、工具调用和用量沿用 SDK 解析，不为兼容回退重复发起请求；多条增量事件、损坏 JSON 和缺少完整消息的封装不会被截取为成功响应。该回退面向 Chat Completions 普通调用与函数调用，不绕过 Responses 或原生 `response_format` 的专用校验。流式与非流式公开思考文本优先读取非空字符串 `reasoning_content`，再读取字符串别名 `reasoning`；不拼接签名、密文或未知扩展。是否向用户展示思考仍受 Agent 的思考设置控制。

模型的 `availability_failure`、`availability_checked_at` 记录实际调用确认的访问故障及核验时间，与 `verified_capabilities` 独立。供应商返回 404、401、403 时，分别记录 `model_not_found`、`model_authentication`、`model_access_denied`，并阻断该模型的新选择与对话提交，页面显示受控原因及修复路径。网络故障、限流和服务端临时错误不会单独形成持续阻断。管理员核对模型部署、实际路由、接口地址和当前凭据授权，修复后重新测试文本调用；测试成功解除阻断，原 Agent 和会话可以继续使用。

密钥和自定义请求头加密保存，接口只返回是否已配置，不回传明文。模型测试和 Runtime 调用写入 `yp_model_call_record`，记录可得的用量、耗时、估算成本及脱敏故障类别，不保存提示词、回答、推理内容或凭据。估算成本依赖上游用量和配置单价，不等于供应商账单。只有启用且符合能力要求的模型可用于 Agent；活跃 Agent 引用模型时，管理接口拒绝停用或删除模型、修改其外部 ID/能力/窗口，以及停用或改动其供应商连接参数。可先更新 Agent 的默认模型；旧会话的模型切换边界见[会话内模型切换](runs-and-sessions.md#会话内模型切换)。

管理员配置供应商和模型并完成文本能力验证后，登录用户通过 `GET /api/v1/models/available` 获取新建 Agent 的默认模型选项，再将所选模型 ID 提交给 Agent 创建接口。选项返回模型 ID、显示名称、上下文窗口、`selectable` 和 `notice`；供应商与模型须均启用，已配置的窗口须容纳 Agent 默认输出上限和最低输入空间。未配置窗口的模型仍可列出，Agent 创建或编辑时按实际输出上限再次校验。完整模型目录及供应商配置仅管理员可访问；供应商接口地址、凭据、接口版本或区域实际变化时会重置关联模型验证与访问故障记录；名称、备注、超时修改，以及规范化后相同的地址或相同凭据更新均保留验证。自定义请求头名称按大小写不敏感比较。未通过文本能力验证的模型通常不提供选项。已确认访问故障的文本模型保留为禁用选项并展示修复指引，避免用户误以为需要重复录入。

登录用户可用 `GET /api/v1/agents/share-targets?query=` 按用户名至少 2 字符前缀搜索共享对象；结果只含本人以外有效用户的 ID、用户名，最多 20 条。Agent 所有者选定对象后，再调用 `POST /api/v1/agents/{agent_id}/shares` 授权；撤权调用 `DELETE /api/v1/agents/{agent_id}/shares/{user_id}`。授权时服务端检查 Agent 所有权与目标用户状态；撤权检查 Agent 所有权和现有授权，并请求取消接收方未结束的运行。共享用户只取得 Agent 安全摘要，自己的会话与文件仍独立。
