# 数据库迁移与测试

执行数据库结构检查、定向回归和静态检查。

[文档总览](README.md) · [项目首页](../README.md)

## 数据库与测试

SQLAlchemy 模型在 `app/models` 注册，Alembic 以其元数据管理业务表。修改模型后新增迁移；升级目标库前先备份并核对当前版本，再运行 `uv run alembic upgrade head` 和 `uv run alembic check`。应用启动时会检查业务模型需要的表和列；缺少迁移会在启动阶段报错。业务数据可选择 SQLite、MySQL 或 PostgreSQL；Checkpointer 独立配置。跨数据库场景需要对应的独立测试环境，测试结果须注明实际后端与跳过项。

常用验证命令：

```bash
uv run pytest -q
uv run alembic check
uvx ruff check app ypoint_cli migrations tests scripts sandbox local_run.py
uvx ruff format --check app ypoint_cli migrations tests scripts sandbox local_run.py
```

`ypoint_cli/` 是统一命令实现。部署测试通过 `python -m ypoint_cli` 验证真实命令入口，CLI 专项另覆盖安装后的 `yp` 和外部工作目录调用。参数、打包配置及标准库模块入口见[命令行说明](cli.md)。

代码与提交约定见[贡献指南](../CONTRIBUTING.md)。

## 按变更范围验证

先运行覆盖当前变更的测试文件，例如部署入口修改可运行：

```bash
uv run pytest -q tests/test_cli.py tests/test_deploy_all.py tests/test_deployment.py tests/test_dev_launcher.py tests/test_sandbox_image_retention.py
```

前端类型与生产构建：

```bash
npm --prefix web ci
npm --prefix web run build
```

`web/package.json` 中的 `verify:*` 是按功能划分的浏览器脚本，需要可访问的前端地址与本机 Chrome/Playwright 环境。按脚本说明设置 `WEB_BASE_URL`，只运行与变更相关的项目。多数页面验证使用拦截 API，不代表真实模型或后端全流程联调通过。

数据库方言、真实模型、Docker、OCR 或对象存储相关测试需要对应依赖或显式启用；跳过项应与通过项分别记录。优先使用临时数据库和文件目录，不直接用个人业务数据执行破坏性场景。

纯文档变更核对相对路径、锚点、代码块和实际命令入口即可，无需为排版变更运行整个业务测试集。


## MCP 连接与 stdio 验证

共享连接、工具绑定、HTTP/HTTPS 及自定义端口、内网/回环/保留地址解析、真实回环 HTTP 服务的官方 SDK 发现与调用、固定 IP 与来源校验、凭据撤销和受控子进程测试：

```bash
uv run pytest -q tests/test_mcp_connections.py tests/test_mcp_connection_migration.py tests/test_tool_display_names.py tests/test_user_tools.py tests/test_m51_governance.py
```

MCP 执行许可、分层审批、只读声明与迁移专项：

```bash
uv run pytest -q tests/test_mcp_policy.py tests/test_mcp_execution_policy.py tests/test_mcp_policy_migration.py tests/test_tool_approval_policy.py tests/test_tool_arguments.py tests/test_ag_ui.py
```

`test_mcp_policy.py` 覆盖审批矩阵、旧客户端请求与历史快照、连接收紧取消、只读确认绑定及共享边界；使用官方 SDK 与受控 HTTP MCP 服务验证发送前结构校验和发送后响应丢失，并确认外层超时不丢失未发送证据。`test_mcp_execution_policy.py` 使用真实执行图和临时检查点验证审批恢复、普通写成功、确定未发送、结果未知及换调用 ID 防重。它们不使用真实第三方业务服务，也不证明真实服务的幂等契约已验收。

`test_mcp_policy_migration.py` 在隔离 SQLite 验证迁移 `061 → 062 → 061 → 062`、Alembic 结构一致性、旧数据映射及活动运行回退阻断，不改动个人业务库。MySQL/PostgreSQL 的结构变更仍须在对应隔离环境验收。迁移与审批行为见 [MCP 审批声明与可调用状态](tools.md#mcp-审批声明与可调用状态)。

真实 Docker 场景默认跳过。维护者可构建仅含标准库协议夹具的镜像，再以固定 ID 启用；该夹具用于发现、调用、错误、超时及取消测试，不是业务 MCP 服务，也不访问真实供应商：

```bash
docker build -f tests/fixtures/Dockerfile.mcp-stdio -t y-point-mcp-test:local tests/fixtures
YPOINT_TEST_MCP_STDIO_IMAGE="$(docker image inspect y-point-mcp-test:local --format '{{.Id}}')" \
  uv run pytest -q tests/test_mcp_connections.py
```

浏览器验证覆盖连接管理、反复打开及嵌套编辑的弹窗点击、未保存草稿保留、发现远端函数后创建 Tool、保存失败重试与目录刷新，以及中文/空格显示名的创建、改名和稳定模型标识；桌面/手机场景均使用受控 API：

```bash
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:mcp
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:tool-approvals
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:agents
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:ag-ui-submit
```

审批脚本覆盖 MCP 模式保存与脚本旧审批开关；Agent 脚本覆盖 20 个 Tool 选择及可调用状态，提交脚本覆盖本轮强制 MCP 审批及桌面/手机 POST SSE。上述浏览器验证均使用受控 API，不替代真实后端或第三方 MCP 的联调。

前端端口以 `make dev` 输出为准。配置、权限和禁网限制见 [MCP Tool](tools.md#mcp-tool)。


## MCP 鉴权、同步与版本升级验证

```bash
uv run pytest -q tests/test_mcp_auth_sync.py tests/test_mcp_sync_migration.py tests/test_mcp_connections.py tests/test_mcp_policy.py tests/test_agents.py tests/test_tool_display_names.py
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:mcp-sync
```

`test_mcp_auth_sync.py` 在隔离 SQLite 通过真实 API 验证只预览不授权、发布部分失败、同 ID 顺序及并发重试、过期预览冲突、只读重新确认、租户撤销、远端移除停用，以及 Agent 普通编辑保留版本、显式升级与新旧 Run 快照。官方 MCP SDK 与受控 FastMCP HTTP 服务检查无主密钥、Bearer、API Key 三种方式下握手、发现和正式调用的实际请求头，不访问第三方业务服务。

`test_mcp_sync_migration.py` 实际执行 `062 → 063 → 062 → 063` 和 Alembic 结构检查，并验证新鉴权/范围撤销不能静默回退。数据库使用临时目录，MySQL/PostgreSQL 方言需另行验收。浏览器专项在桌面/手机检查密钥不回填、Header 保留/替换/删除、四类差异、只读确认、发布部分失败稳定重试、停用选择和 Agent 显式升级；页面 API 为受控响应，不代表真实第三方部署已验收。既有 `verify:mcp`、`verify:tool-approvals`、`verify:agents` 分别复验对应兼容流程。

## MCP 连接关联与共享验证

```bash
uv run pytest -q tests/test_mcp_connection_sharing.py tests/test_mcp_sharing_migration.py tests/test_session_models.py tests/test_skill_packages.py
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:mcp-sharing
```

`test_mcp_connection_sharing.py` 使用真实 HTTP 接口和隔离 SQLite，覆盖 HTTP/stdio 连接的 17 工具选择 10 个、重复去重、版本冲突、非法子集、指定用户与摘要脱敏、凭据借用独立授权、正式 AG-UI 提交固定快照、Skill 外部依赖、显式升级同时更新直接/连接绑定、旧 Run 保留版本、远端新增不扩大授权、子集/凭据收紧取消活动运行、撤销重授权及并发修改冲突。远端目录使用受控返回，AG-UI 只替换持续 SSE 响应适配以避免等待 Worker；不调用真实第三方服务或 stdio 容器，不证明目标部署已验收。

`test_mcp_sharing_migration.py` 在临时 SQLite 实际执行 `063 → 064 → 063 → 064`、Alembic 结构检查，并验证历史直接绑定/执行摘要不变，以及共享、连接绑定、Run 授权来源三类回退保护。其他数据库方言需独立验收。

`verify:mcp-sharing` 在 1280px/375px 使用受控 API 验证批量选择后保存 10 项、保留旧版本、阻断工具禁选、目录失败保留与修复、独立凭据授权、冲突反馈及带授权版本撤销；截图保存在 `/tmp/yp-mcp-sharing-{picker,manager}-{1280,375}.png`。它与既有 `verify:agents`、`verify:mcp`、`verify:mcp-sync` 分别验证对应界面，不替代真实后端部署联调。

## MCP 批次 D：逐项审批与 Run 会话

```bash
uv run pytest -q tests/test_mcp_approval_continue.py tests/test_mcp_run_sessions.py tests/test_mcp_run_sessions_migration.py
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:approvals
```

`test_mcp_approval_continue.py` 使用真实 DeepAgents 图、隔离业务库和检查点，验证混合批准/拒绝只执行指定项、拒绝后换调用 ID/参数不执行、旧 reject 与显式 stop 仍取消、完整动作绑定、精确重复决定、冲突、过期、脱敏预览及最终执行记录。`test_ag_ui.py` 同时验证正式 POST 的 approve、reject_continue、resolve 和公开历史投影，模型上游由受控响应替代。

`test_mcp_run_sessions.py` 使用官方 SDK 1.30.0 与真实回环 HTTP FastMCP、真实 stdio 子进程管道验证连续调用和跨审批状态、用户/Run 隔离、同连接串行、排队后重验取消与授权、Worker 归属路由、空闲与累计预算、资源配额、撤权、退出和 HTTP 404。慢调用在远端真实收到 `notifications/cancelled`，请求 ID 必须对应原工具请求；stdio 回收断言进程退出、管道任务和临时目录清理，保留禁网命令。正式 Worker 工具链另验证外部写已发送才超时的 UNKNOWN 审计，不能把传输清理当成安全补做依据。

HTTP 失主回收使用实际加密凭单和另一 Worker 的正式清理函数，验证只发 DELETE、失败节流和重试、不再握手/执行业务、不明文保存 URL 或会话 ID。它不是实际杀死 Docker Worker 后的多机验收；stdio 容器强杀后的标签核对和 MySQL/PostgreSQL、真实业务 MCP 仍须在对应环境执行。初始化返回与凭单落库的崩溃窗口、远端不支持 DELETE 或持续拒绝回收时的边界按 [会话契约](tools.md#run-内-mcp-会话与跨审批恢复) 记录，不将服务端资源释放宣称为可无条件保证。

迁移测试只操作临时 SQLite，验证 `064 → 065 → 064 → 065`、旧拒绝保留、Alembic 结构及新版决策/资源归属/待清理凭单三类回退阻断，不迁移个人业务库。浏览器覆盖 1280/375px、单项拒绝继续、逐项决定必须全选、显式停止、原有锁定提交、SSE、冲突、过期和加载恢复；浏览器使用受控 API。

本机批次 D 最终记录（2026-11-23）：21 个相关测试文件 **328 passed / 9 skipped / 8 warnings**；9 项为 Docker 条件测试，8 个为既有弃用接口警告。4 个迁移专项通过，065 最终补验通过；前端生产构建与 verify:approvals、verify:ag-ui-submit、verify:tool-approvals 通过。完整交付边界见 [批次 D 记录](../todo-list.md#10-批次-d-实施记录2026-11-23)。

## MCP 批次 E：静态上下文预算与大结果

```bash
uv run pytest -q tests/test_mcp_context_budget.py tests/test_mcp_results.py tests/test_mcp_results_migration.py
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:mcp-results
```

预算专项使用正式图、授权过滤、摘要和检查点，模型上游受控：验证 17 个小工具真正绑定并完成、单个超大 Schema 在上游调用前拒绝、中文 Schema/系统/历史/附件计量、最终 Schema 字节与实际绑定集合一致，以及脱敏诊断不含请求正文。沿用长会话、输出留空和供应商上下文错误专项，避免摘要恢复和输出预留出现兼容回退。

结果专项使用真实 TCP 的官方 HTTP FastMCP、正式 Worker/审批/调用审计：一次创建返回大量数据时，正常保存和配额失败都只执行一次并保留成功；保存超时之前先持久化成功证据，整轮可以超时但调用仍为成功。远端错误或传输接收中断时外部写仍为 UNKNOWN。验证一万行响应的有界合法 JSON 预览、连续片段拼回后的 SHA-256/UTF-8/JSON、单次响应范围和远端游标、不盲目当作全量；验证跨用户/Run、摘要不符、无效偏移、取消、过期、保存失败、单 Run 配额并发和正文清理。

本地与私有 S3 路径使用同一个产物发布/读取/清理模块；S3 测试使用内存客户端，验证加密参数、对象存取和删除，不代表真实对象存储服务或多机并发已验收。迁移只操作临时 SQLite，验证 `065 → 066 → 065 → 066`、旧来源为空、结构一致及已有结果来源时拒绝回退。D 的迁移专项在当前 head 比对元数据，再回到 065 验证其原有保护。

浏览器 `verify:mcp-results` 在 1280/375px 通过实际组件验证预算明细、结果标识、固定摘要分页、分段失败/重试/EOF、私有下载、过期及文本不执行；使用受控 API。既有预览、审批及 AG-UI 提交回归独立执行，不将浏览器受控响应当作真实后端部署联调。完整交付记录见 [批次 E](../todo-list.md#11-批次-e-实施记录2026-11-23)。

本机批次 E 最终记录（2026-11-23）：34 个相关文件 **454 passed / 13 skipped / 9 warnings**；E 契约专项 **18 passed**（含 066 迁移），五个 MCP 迁移专项 **5 passed**。跳过项为 Docker/stdio 条件 9 项、真实 Ollama 长会话 2 项、MySQL 配额 2 项；未计作通过。前端生产构建、1280/375px 新结果回归、既有预览 202 断言、审批及 AG-UI 提交回归通过；部署、真实外部业务/对象存储、其他数据库与多机验收仍待执行。

## MCP 批次 F：动态搜索与整体验收

本机验证日期：2026-11-23。新增 `tests/test_mcp_tool_search.py` 使用正式 DeepAgents 图、检查点、运行审计和实际 HTTP MCP，模型响应受控。覆盖 17 个大 Schema 的按需绑定/换组、猜测未加载工具不执行、单个巨型 Schema 仍被预算拒绝、固定版本与双 `search` 分页、跨 Run 选择隔离、搜索期间撤权、审批后新运行时恢复、拒绝后重新搜索不可绕过、外部写报错/传输截断保持 UNKNOWN。七类实际模型适配器的普通函数 Schema 绑定通过，无真实供应商请求。

最终相关专项 **51 passed**；其中动态搜索专项 **15 passed**，另外覆盖静态预算、逐项审批、供应商适配与 Skill 依赖兼容。复现入口：

```bash
uv run pytest -q tests/test_mcp_tool_search.py tests/test_mcp_context_budget.py tests/test_mcp_approval_continue.py tests/test_provider_backends.py tests/test_skill_packages.py
```

A—F 相关回归 **406 passed / 13 skipped / 9 warnings**。该次收集包含动态搜索前 8 项行为用例；后增加的 7 项供应商绑定已包含于上述 51 项专项，计数不重复相加。跳过项为环境条件测试，不计作通过；警告为既有发现接口弃用和重复 ZIP 条目。命令：

```bash
uv run pytest -q tests/test_mcp*.py tests/test_tool*.py tests/test_user_tools.py tests/test_worker_tools.py tests/test_ag_ui.py tests/test_long_context.py tests/test_runtime_limits.py tests/test_runtime_checkpoint.py tests/test_model_context_errors.py tests/test_optional_model_timeout.py tests/test_model_connection_retry.py tests/test_skill_packages.py tests/test_skills.py tests/test_skill_reports.py tests/test_artifacts.py tests/test_artifact_preview.py tests/test_artifact_intent.py tests/test_artifact_read_feedback.py tests/test_generated_artifact_reuse.py tests/test_artifact_cleanup.py tests/test_run_calls.py tests/test_agents.py tests/test_agent_sessions.py tests/test_m51_governance.py
```

062—066 五个迁移专项 **5 passed**，全部为临时 SQLite 的往返、旧数据与回退保护，不修改业务库：

```bash
uv run pytest -q tests/test_mcp_policy_migration.py tests/test_mcp_sync_migration.py tests/test_mcp_sharing_migration.py tests/test_mcp_run_sessions_migration.py tests/test_mcp_results_migration.py
```

前端 TypeScript 与生产构建通过，保留既有大于 500 kB 包体提示。`verify:mcp-results` 在 1280/375px 验证按需加载固定/实际绑定数量、有界结果分页、重试、EOF、下载、过期及安全文本；`verify:agents` 验证 17 工具以上选择及原页面流程，`verify:approvals` 验证新旧审批、逐项决定、SSE、冲突和错误恢复。均使用受控 API，不代替实际账号联调。入口如下，需先启动专用前端开发服务：

```bash
WEB_BASE_URL=http://127.0.0.1:5179 SCREENSHOT_DIR=/tmp/ypoint-mcp-f-screenshots npm --prefix web run verify:mcp-results
WEB_BASE_URL=http://127.0.0.1:5179 npm --prefix web run verify:agents
WEB_BASE_URL=http://127.0.0.1:5179 npm --prefix web run verify:approvals
```

截图中的加载数量和预算排版已检查桌面/手机；Ruff、格式、脚本语法、空白与 134 处本地文档引用检查通过，本轮专用 5179 验证服务已关闭。F 不新增迁移，默认静态模式保持兼容，动态模式通过 `MCP_TOOL_SCHEMA_MODE=dynamic` 启用。目标数据库方言、真实 Docker/S3、真实模型与业务 MCP、多机恢复仍待 [验收矩阵](mcp-acceptance.md) 中的目标环境验证；不得将本机结果视为 Issues #1/#3/#4 全部验收完成。


## MCP-07：可调用状态与页面错误恢复

2026-11-23：补齐创建/修改/详情/目录/连接分组的同源预检快照；判断时间独立于 `callability` 语义。后端定向回归 **86 passed**，使用隔离 SQLite 与受控远端发现，覆盖普通写需审批、所有者只读确认、共享收紧、策略变更、停用、凭据未借用/已借用及摘要脱敏。结果包括此前 23 项专项，不重复累计：

```bash
uv run pytest -q tests/test_user_tools.py tests/test_mcp_auth_sync.py tests/test_mcp_execution_policy.py tests/test_mcp_policy.py tests/test_mcp_connection_sharing.py tests/test_agents.py
```

生产构建通过，保留已有大于 500 kB 包体提示。以下浏览器脚本使用受控 API，覆盖 1280/375px：新增可调用状态脚本验证可展开状态说明、目录/详情失败重试、已有结果/选择/草稿保留、刷新后的版本冲突、关闭详情后的迟到响应隔离、旧版绑定和失效关联修复；原有共享、工具审批、Agent 页面流程同步回归。需要先启动专用前端服务，截图写入临时目录：

```bash
WEB_BASE_URL=http://127.0.0.1:5179 WEB_SCREENSHOT_DIR=/tmp/ypoint-mcp07-screenshots npm --prefix web run verify:callability
WEB_BASE_URL=http://127.0.0.1:5179 npm --prefix web run verify:mcp-sharing
WEB_BASE_URL=http://127.0.0.1:5179 WEB_SCREENSHOT_DIR=/tmp/ypoint-mcp07-screenshots npm --prefix web run verify:tool-approvals
WEB_BASE_URL=http://127.0.0.1:5179 AGENT_SCREENSHOT_DIR=/tmp/ypoint-mcp07-screenshots npm --prefix web run verify:agents
```

状态刷新不修改草稿的基准版本，不将当前发布版本的预检套用到旧绑定。截图检查桌面/手机布局；Ruff、Python 格式、脚本语法和改动空白检查通过。本次无需新迁移；未对业务库执行 062—066。目标环境配置与迁移状态的只读核对见 [验收矩阵](mcp-acceptance.md#mcp-07-补齐后的环境核对)，不代替真实前后端账号、供应商、Docker/S3 或第三方业务验收。


## AG-UI Issue #2：版本提交与公开历史缓存

```bash
uv run pytest -q tests/test_ag_ui.py tests/test_ag_ui_version_history.py tests/test_ag_ui_history_migration.py tests/test_agent_sessions.py tests/test_session_models.py tests/test_mcp_results_migration.py
npm --prefix web run build
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:ag-ui-submit
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:ag-ui-version
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:session-models
WEB_BASE_URL=http://127.0.0.1:5173 npm --prefix web run verify:approvals
```

测试使用独立 SQLite、真实 ASGI HTTP/登录/数据库事务与正式协议投影；Worker 输出由受控执行代替，不调用真实模型或外部工具。覆盖无历史重建的版本提交、原请求跨后续轮重试、非法版本、旧模式与 null、历史消息 ID 冲突、并发版本竞争、跨用户拒绝、终态缓存复用与失效、早期审批截面和 067 升降级。

多轮夹具包含 12 个 Run、240 个文本事件，以「首次投影 12 次，跨数据库会话热读取投影 0 次，公开历史完全相同」验证缓存收益；不以机器相关的毫秒阈值判定正确性。缓存仍需读取完整消息和其他展示元数据，不承诺历史接口恒定耗时。

前端 `verify:ag-ui-submit` 在桌面/手机验证仅发送新消息、POST 前不读取历史、真实 POST SSE 及无重复 GET 观察；`verify:ag-ui-version` 验证网络失败复用原请求身份、版本冲突保留草稿且不自动重发、手动重发使用刷新后的版本和新请求身份。浏览器接口受控，不代表真实模型生成效果。

2026-11-24 本机结果：上述六文件后端综合回归 **55 passed**；前端 TypeScript 和生产构建通过，保留既有 >500 kB 包体提示。`verify:ag-ui-submit`、`verify:ag-ui-version` 桌面/手机通过；`verify:session-models` **59 项断言**、`verify:ag-ui` **26 项断言**及 `verify:approvals` 回归通过。Ruff、格式、脚本语法和变更空白检查通过。

本地 SQLite 已在无活动运行时备份并从 066 升级至 **067**；Alembic 结构检查、SQLite 完整性检查通过，升级前原有 57 张表的记录数保持不变。API/Worker 健康与前端返回 200，备份保存在被忽略的 `.run-private/backups/`，仅本机使用。不据此声称 MySQL/PostgreSQL 迁移、真实模型生成效果或目标部署延迟已验证。未提交/推送 Git 或修改 Issue。
