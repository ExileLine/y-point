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

健康检查接口为 `/api/v1/health`，接口文档为 `/docs`。启动时会检查数据库连接；Redis 是否初始化由 `REDIS_ENABLED` 控制。PostgreSQL 和 MySQL 的迁移及业务兼容性仍需分别测试，不能仅凭数据库连接配置视为已支持。

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

运行 `uv run alembic upgrade head` 升级到 `003`。管理员可管理 `/api/v1/providers` 供应商连接和 `/api/v1/models` 可选模型，普通用户不能访问这些配置。供应商类型支持 `openai_compatible`、`openai`、`anthropic`；模型保存外部 ID、显示名、能力标签、上下文窗口及每百万 Token 的输入/输出价格，价格币种默认为 `USD`。未填写的能力保存为空列表，人工填写或导入的能力不会自动标为已验证。

使用 API 密钥或自定义请求头前，在私有环境配置中设置独立的 `CREDENTIAL_ENCRYPTION_KEY`。它必须是 Fernet 密钥，可用 `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` 生成。开发环境的 `.env.development.local` 已设置本机密钥且被 Git 忽略。请保留并备份该密钥：更换或丢失后，已保存的供应商凭据无法解密。API 只返回 `has_api_key` 和 `has_custom_headers`；`PUT` 中省略凭据字段会保留原值，明确传入 `null` 会清除对应凭据。

### 模型录入流程

1. `POST /api/v1/providers` 创建连接。供应商地址支持 HTTP 和 HTTPS；HTTP 会明文传输 API 密钥和自定义请求头，仅适合可信网络。
2. `POST /api/v1/providers/{provider_id}/test` 检查上游 `/models`，返回 `available`、`latency_ms`、`model_count`、模型 ID 列表 `models`、可选名称映射 `model_names` 和失败类别。`GET /api/v1/providers/{provider_id}/available-models` 返回可供选择的 `external_id`、建议 `display_name` 和 `already_added`；上游未提供名称时以 ID 作为建议名称。
3. 选择模型后调用 `POST /api/v1/models/import`，一次最多提交 100 个模型。请求体示例：

   ```json
   {"provider_id": 1, "models": [{"external_id": "gpt-4o-mini", "capabilities": ["text"]}]}
   ```

   `display_name` 省略时使用模型 ID；能力、上下文和价格可留空。响应的 `created` 包含新增或恢复的模型，`skipped_existing` 列出已录入的模型 ID；重复提交不会重复建模。手动录入列表外模型仍可使用 `POST /api/v1/models`。
4. 按需调用 `POST /api/v1/models/{model_id}/test` 实测该模型的最小文本生成请求，再通过 `POST /api/v1/models/{model_id}/default` 指定默认模型。模型测试只在管理员主动请求时运行，可能产生少量费用。

上游调用失败时，连接测试与发现接口返回故障类别和空列表，不写入模型，也不回传上游原始错误或凭据。模型测试通过 OpenAI/兼容接口的 `/chat/completions` 或 Anthropic 的 `/messages` 请求，返回 `available`、`latency_ms`、`upstream_status_code` 和脱敏的 `failure`，不返回生成内容。它目前只验证文本聊天调用，不自动验证视觉、工具或流式输出；非文本模型可能返回 `invalid_request`。供应商下仍有有效模型时不能删除连接，停用连接会清除其模型的默认标记。

M2 目前已通过模拟上游的权限、成功与失败场景测试；真实供应商凭据下的推理、无模型列表接口的适配、非文本模型测试、生产网络边界、PostgreSQL/MySQL 迁移及并发默认模型选择仍待验证。

## 数据库迁移

Alembic 使用 `app.models.base.Base.metadata`，通过 `app.models` 注册模型。首个迁移 `001_initial.py` 创建现有 `Admin` 用户表和 `ApsTask` 定时任务表，表名遵循 `CustomBaseModel` 的前缀配置。`Admin` 是用户模型，后续认证和权限字段应在该模型上扩展，并以新增迁移同步数据库结构。修改模型后可运行 `uv run alembic check` 检查迁移差异。

`002_auth.py` 在 `yp_admin` 上新增角色、令牌版本和最近登录时间及用户名/邮箱唯一索引，另建刷新会话、登录尝试和审计表。已有数据库若存在重复用户名或邮箱，升级前需先清理重复数据，否则唯一索引创建会失败。

认证和审计表只保存用户 ID，不建立数据库外键。业务层在认证与查询时检查用户状态、权限及会话归属；后续删除和清理流程需处理相关记录。表与字段定义均带中文注释。

## Agent 运行时验证

项目锁定 `deepagents==0.7.29`。运行 `uv run pytest -q tests/test_deepagents_spike.py` 验证本地假模型的工具、Skill、流式事件、检查点、审批恢复与取消。实测版本、事件形态和待验证的真实模型/持久化能力见 `docs/deepagents-compatibility.md`。
