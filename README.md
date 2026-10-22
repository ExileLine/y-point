# Y-Point 服务端

Y-Point 智能助手平台后端。需求见 `y-point-dev.txt`，开发与验收清单分别见 `y-point-server-todo.md`、`y-point-web-todo.md`。

## 本地开发

使用 Python 3.12，并通过 `uv sync --group dev` 安装锁定的依赖。默认加载 `.env.development`；数据库类型、连接参数和 Redis 开关以对应环境配置为准。当前开发配置使用项目根目录的 SQLite 数据库 `y_point.db`。`.env.example` 仅说明配置项，不是可直接启动的配置文件；密钥应放在私有环境配置或环境变量中。

开发环境可在不提交的 `.env.development.local` 中设置 `SECRET_KEY`，它会覆盖 `.env.development` 中的占位值。使用至少 32 字节的随机密钥；修改后需重启服务，旧访问令牌会失效。

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
uv run pytest -q
```

健康检查接口为 `/api/v1/health`，接口文档为 `/docs`。启动时会检查数据库连接；Redis 是否初始化由 `REDIS_ENABLED` 控制。M2 已在独立 MySQL 测试库验证迁移与供应商/模型 HTTP 流程；PostgreSQL 及其他模块的跨数据库兼容性仍需验证。

## M1 认证与用户管理

先运行 `uv run alembic upgrade head`，再创建首位管理员。命令会交互式读取密码，不将密码写入命令历史；密码需为 12 位以上、包含大小写字母和数字，UTF-8 编码不超过 72 字节。已存在有效管理员时，该命令会拒绝再次初始化。

```bash
uv run python -m app.cli.create_admin admin --mail admin@example.com
```

认证接口：`POST /api/v1/auth/login`（`identifier` 可为用户名或邮箱）、`POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout`、`GET/PUT /api/v1/auth/me`、`POST /api/v1/auth/password`。登录返回有效期 7 天的 Bearer JWT 和有效期 30 天的刷新令牌；刷新令牌每次使用后轮换，服务端只保存其 SHA-256 摘要。访问令牌每次请求都会核对数据库中的用户状态、令牌版本和会话，登出、改密、重置密码、停用或变更角色可立即撤销旧令牌。

M1 路由的成功结果使用 `api_response` 返回 `code`、`message` 和可选的 `data`；业务错误使用 `CustomException`，保留对应 HTTP 状态和业务码。退出、改密及重置密码成功时返回 HTTP 200 与成功消息。路由的 OpenAPI `summary` 使用中文。

在 `/docs` 中先调用登录接口，复制响应 `data.access_token`，点击页面右上角 **Authorize** 并只粘贴令牌本身。Swagger UI 会自动添加 `Authorization: Bearer <token>`；直接调用 API 时则需自行发送完整请求头。两个 `PUT` 更新接口仅修改请求体中提供的字段。

管理员接口：`POST/GET /api/v1/users`、`GET/PUT /api/v1/users/{user_id}`、`POST /api/v1/users/{user_id}/reset-password` 和 `GET /api/v1/users/audit/events`。普通用户只能读取或修改自己的资料；管理员可创建、查看、启停用户及调整角色。用户创建和密码重置由管理员设置初始密码，邮件邀请流程尚未实现。连续 5 次登录失败会在 15 分钟内限制该账号和来源 IP。审计记录包含操作、操作者、目标用户和时间，不保存密码或明文令牌。

`SECRET_KEY` 用于签署 JWT，本地配置也应替换为至少 32 字节的随机值；生产和预发布环境的短密钥会阻止服务启动。不要在前端存储或记录明文刷新令牌。当前登录限流以数据库为准，跨数据库高并发领取及 PostgreSQL/MySQL 迁移尚未验证。

## M2 供应商连接与模型目录

M2 的独立开发项已基本完成，可以继续开发 M3；全量验收尚未完成。已交付供应商与模型管理、加密凭据、模型发现和批量导入、最小文本测试、逐项能力探测、脱敏调用记录、用量与估算成本，以及受限故障重试。本地 OpenAI 兼容连接和 Ollama 真实模型已验证；Azure OpenAI、Gemini Developer API、AWS Bedrock 完成适配及模拟测试，仍需真实云服务验收。OpenAI 官方、Anthropic 和 PostgreSQL 的真实验收按当前计划延期；Agent 引用限制、运行时调用记录与备用模型切换归入 M3/M4。详细完成项和待验收项见 `y-point-server-todo.md`。

运行 `uv run alembic upgrade head` 升级到 `006`。管理员可管理 `/api/v1/providers` 供应商连接和 `/api/v1/models` 可选模型，普通用户不能访问这些配置。`GET /api/v1/providers/kinds` 返回各供应商适用的连接字段和建议地址；当前支持 `openai_compatible`、`openai`、`anthropic`、`ollama`、`azure_openai`、`gemini`、`bedrock`。Ollama 可用 `http://127.0.0.1:11434/v1` 且无需 API 密钥。模型保存外部 ID、显示名、能力标签、上下文窗口及每百万 Token 的输入/输出价格，价格币种默认为 `USD`。未填写的能力保存为空列表，人工填写或导入的能力不会自动标为已验证。

使用 API 密钥或自定义请求头前，在私有环境配置中设置独立的 `CREDENTIAL_ENCRYPTION_KEY`。它必须是 Fernet 密钥，可用 `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成。开发环境的 `.env.development.local` 已设置本机密钥且被 Git 忽略。请保留并备份该密钥：更换或丢失后，已保存的供应商凭据无法解密。API 只返回 `has_api_key` 和 `has_custom_headers`；`PUT` 中省略凭据字段会保留原值，明确传入 `null` 会清除对应凭据。

### 模型录入流程

1. `POST /api/v1/providers` 创建连接。供应商地址支持 HTTP 和 HTTPS；HTTP 会明文传输 API 密钥和自定义请求头，仅适合可信网络。
2. `POST /api/v1/providers/{provider_id}/test` 检查供应商的模型发现能力，返回 `available`、`latency_ms`、`upstream_status_code`、`model_count`、模型 ID 列表 `models`、可选名称映射 `model_names` 和失败类别。`GET /api/v1/providers/{provider_id}/available-models` 返回可供选择的 `external_id`、建议 `display_name` 和 `already_added`；上游未提供名称时以 ID 作为建议名称。
3. 选择模型后调用 `POST /api/v1/models/import`，一次最多提交 100 个模型。请求体示例：

   ```json
   {"provider_id": 1, "models": [{"external_id": "gpt-4o-mini", "capabilities": ["text"]}]}
   ```

   `display_name` 省略时使用模型 ID；能力、上下文和价格可留空。响应的 `created` 包含新增或恢复的模型，`skipped_existing` 列出已录入的模型 ID；重复提交不会重复建模。手动录入列表外模型仍可使用 `POST /api/v1/models`。
4. 按需调用 `POST /api/v1/models/{model_id}/test` 实测最小文本请求，或向 `POST /api/v1/models/{model_id}/capabilities/test` 提交 `{"capability":"text"}`、`{"capability":"streaming"}`、`{"capability":"tools"}`、`{"capability":"structured_output"}`、`{"capability":"vision"}` 或 `{"capability":"reasoning_summary"}` 逐项验证能力；最后通过 `POST /api/v1/models/{model_id}/default` 指定默认模型。探测只在管理员主动请求时运行，可能产生少量费用。

如果供应商没有 `/models` 接口，连接测试和发现接口会返回 `model_list_unavailable`，不会把它误判成密钥无效。此时用 `POST /api/v1/models` 手动录入已知的供应商模型 ID，再调用 `POST /api/v1/models/{model_id}/test` 进行可配置的最小推理探测；响应包含可用性、耗时、上游状态与脱敏故障类别。模型列表接口不可用时，平台无法代替供应商枚举模型。

Azure OpenAI 连接填写资源根地址（如 `https://example.openai.azure.com`）、API Key 和 API 版本；模型的 `external_id` 填部署名称。数据面密钥不能列出部署，发现接口固定返回 `model_list_unavailable`，需手动录入并调用模型测试。Gemini Developer API 连接填写 API Key，服务根地址默认 `https://generativelanguage.googleapis.com`，版本默认为 `v1beta`；发现接口翻页读取支持 `generateContent` 的模型。Bedrock 连接填写 AWS Region，不填写 API Key；服务使用进程的 AWS 凭据链和该区域的 Bedrock 控制面/Converse API。发现接口列出文本基础模型，推理配置文件 ID 可手动录入并单独探测。三类连接目前通过模拟上游和 SDK 配置测试，真实云服务调用待验收。

上游调用失败时，连接测试与发现接口返回故障类别和空列表，不写入模型，也不回传上游原始错误或凭据。模型测试返回 `available`、`latency_ms`、`upstream_status_code` 和脱敏的 `failure`，不返回生成内容。有用量时返回 `input_tokens`、`output_tokens`；仅当用量和每百万 Token 的输入/输出单价均存在时，返回字符串形式的 `estimated_cost` 及 `price_currency`。成本是估算值，不包含供应商折扣或其他计费项目。原有 `/test` 只验证文本聊天调用；逐项接口可验证文本、流式输出、工具调用、固定结构化对象和固定四色图像。公开推理摘要探测仅对 `openai` 类型请求 Responses API 的 `reasoning.summary`，只在完成的响应中存在非空 `summary_text` 时通过；其他类型返回 `unsupported_capability`，不读取或保存隐藏推理内容、公开摘要及正文。该探测目前通过模拟协议测试，尚无 OpenAI 官方真实调用验收。结构化探测采用函数调用模式，验证解析结果是否严格符合固定对象，不代表模型能满足任意业务 JSON Schema。视觉探测通过内嵌 PNG 检查四色象限顺序，不代表复杂图像理解能力；视觉请求最多允许 512 个输出 Token，实际计费由供应商决定。非文本模型可能返回 `invalid_request`。供应商下仍有有效模型时不能删除连接，停用连接会清除其模型的默认标记。

连接、文本和逐项能力探测在上游返回 HTTP `502/503/504` 时会等待约 200 毫秒后最多重试一次；认证失败、无效请求、限流、超时和网络错误不会自动重试。流式探测一旦收到片段，即使随后返回网关错误也不会重试。`latency_ms` 包含等待及重试时间，响应和调用记录只保存最终结果；上游可能对两次请求分别计费。

每次管理员发起的模型文本或逐项能力测试都会写入 `yp_model_call_record`，成功和失败均保存模型/供应商 ID、操作者、耗时、HTTP 状态、脱敏故障类别、可得的 Token 用量与估算成本；不保存提示词、生成正文、推理摘要或凭据。测试响应的 `record_id` 对应此记录。管理员可用 `GET /api/v1/models/{model_id}/calls?page=1&size=20` 分页查看，即使模型随后被逻辑删除，历史仍可查询。逐项探测成功会写入 `verified_capabilities`，失败会撤销该项验证；修改模型能力、模型 ID 或供应商调用配置会清空验证状态。`capabilities_verified` 仅在所有已填写的能力均通过探测时为真；当前可探测文本、流式、工具、结构化输出、视觉和 OpenAI 公开推理摘要。停用的模型或供应商不能发起新测试。Agent 运行时调用记录将在 M3 接入。

`005` 迁移给模型增加全局唯一的默认占位键；并发设置默认模型时，数据库会阻止两个模型同时成为默认项。停用或删除模型及供应商时同步清除占位键。升级前若已有多个默认标记，迁移保留 ID 最大的一个。

运行时可用 `app.services.model_adapter.build_chat_model(model, provider)` 将有效配置转换为 LangChain 聊天模型，支持 OpenAI、OpenAI 兼容协议、Anthropic、Ollama、Azure OpenAI、Gemini Developer API 及 Bedrock；停用或不匹配的配置会被拒绝。已使用本地连接和模型 `id=1` 实测一种 OpenAI 兼容供应商的文本、流式输出、工具调用及 Token 用量；同一模型的最小调用使用错误密钥时被归类为 `authentication`。另以本机 Ollama 的 `deepseek-r1:1.5b` 实测 `/v1/models` 发现 4 个模型、最小文本调用、LangChain 适配调用，以及文本和流式能力探测，均返回用量；该验证未创建业务库记录，也未检查工具或视觉能力。模型列表仅代表服务端列出该 ID，具体能力仍需逐项探测。OpenAI 官方与 Anthropic 的真实调用、Azure/Gemini/Bedrock 的真实云调用、完整 Agent 编排、Agent 调用记录、非文本模型测试、生产网络边界及 PostgreSQL 迁移仍待验证。

本机 `gemma4:31b-cloud` 又完成模型发现、最小文本、文本、流式、工具、固定结构化输出和四色视觉探测，五项能力均返回 Token 用量；错误模型 ID 被归类为 `model_not_found`，公开摘要按 Ollama 类型返回 `unsupported_capability`。此验证不写入业务数据库，可用 `YPOINT_TEST_OLLAMA_GEMMA4=1 uv run pytest -q tests/test_capability_probe.py::test_live_ollama_gemma4` 重复执行。

M2 数据库验证范围：SQLite 开发库已升级到 `006`；独立 SQLite 库完成 `005 → 006 → 005 → 006` 和迁移差异检查。MySQL 先前已在独立临时库验证 `001 → 005`、`005 → 003 → 005`、ORM 读写和 HTTP 流程；本轮另在独立临时库完成 `001 → 006 → 005 → 006` 与两次迁移差异检查，测试库均已删除。PostgreSQL 当前无可用本地服务，按本轮约定延期验证，不能据此声明 PostgreSQL 兼容。MySQL 的负载与生产部署仍需单独验收。

## 数据库迁移

Alembic 使用 `app.models.base.Base.metadata`，通过 `app.models` 注册模型。首个迁移 `001_initial.py` 创建现有 `Admin` 用户表和 `ApsTask` 定时任务表，表名遵循 `CustomBaseModel` 的前缀配置。`Admin` 是用户模型，后续认证和权限字段应在该模型上扩展，并以新增迁移同步数据库结构。修改模型后可运行 `uv run alembic check` 检查迁移差异。

`002_auth.py` 在 `yp_admin` 上新增角色、令牌版本和最近登录时间及用户名/邮箱唯一索引，另建刷新会话、登录尝试和审计表。已有数据库若存在重复用户名或邮箱，升级前需先清理重复数据，否则唯一索引创建会失败。

认证和审计表只保存用户 ID，不建立数据库外键。业务层在认证与查询时检查用户状态、权限及会话归属；后续删除和清理流程需处理相关记录。表与字段定义均带中文注释。

## Agent 运行时验证

项目锁定 `deepagents==0.7.29`。运行 `uv run pytest -q tests/test_deepagents_spike.py` 验证本地假模型的工具、Skill、流式事件、检查点、审批恢复与取消。实测版本、事件形态和待验证的真实模型/持久化能力见 `docs/deepagents-compatibility.md`。
