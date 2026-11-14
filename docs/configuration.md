# 配置与运行限额

查阅附件容量、模型与 Agent 参数、覆盖顺序、长任务设置和排查方法。

[文档总览](README.md) · [项目首页](../README.md)

## 附件容量与运行参数配置

API 与 worker 通过同一套环境配置读取附件容量和单次调用时限。可在 `.env.development.local` 配置本机值，部署时使用环境变量；完整参数见 `.env.example`。配置加载后在进程内缓存，修改后须重启 API 与 worker，前端重新加载页面以读取当前配置。

**对话整轮不设置总倒计时。** 一项正常任务可以多次调用模型、Skill、Tool、MCP 或知识库，只要每次调用在对应时限内完成，就不会因累计运行时间较长被终止。取消、权限复核、租约心跳和明确配置的调用次数/Token/费用预算仍生效；达到图步数上限时以明确的失败原因结束，防止无限循环。

| 配置项 | 默认值 | 生效范围 |
| --- | --- | --- |
| `ARTIFACT_UPLOAD_MAX_BYTES` | `20971520`（20 MiB） | 对话上传单文件字节上限；multipart 请求总限额自动按 5 个文件加 1 MiB 开销计算。 |
| `ATTACHMENT_CONTEXT_MAX_BYTES` | `20971520`（20 MiB） | 本轮附件提取正文及 JSON 元信息的 UTF-8 字节预算；预检查与 worker 共用。 |
| `AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS` | `120` | 新 Agent 和独立 Run 默认的单次模型调用时限；普通生成和历史摘要均受其控制。 |
| `AGENT_MAX_MODEL_TIMEOUT_SECONDS` | `3600` | Agent、独立 Run 及供应商管理允许设置的模型或请求超时上限。 |
| `AGENT_DEFAULT_TOOL_TIMEOUT_SECONDS` | `120` | 新 Agent 和独立 Run 默认的单次工具调用时限。 |
| `AGENT_MAX_TOOL_TIMEOUT_SECONDS` | `3600` | 允许设置的单次工具调用时限上限。 |
| `AGENT_DEFAULT_MAX_OUTPUT_TOKENS` | `4096` | 创建 Agent 或独立 Run 时省略额度采用的单次输出 Token 默认值；Agent 显式传 `null` 可交由供应商决定。 |
| `AGENT_MAX_OUTPUT_TOKENS` | `65536` | Agent 创建、修改时允许填写的最大整数额度；不约束显式 `null`，也不代表供应商实际输出上限。 |
| `AGENT_MODEL_CONNECTION_MAX_RETRIES` | `1` | OpenAI/Anthropic 模型尚未返回数据时，SDK 连接错误的最多重试次数；范围 0–3，0 为禁用。每次尝试均消耗模型调用次数。 |
| `AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS` | `1` | 连接重试前等待秒数，范围 0–30；所有尝试和等待共用原单次模型时限。 |
| `MCP_CONNECT_TIMEOUT_SECONDS` | `10` | MCP HTTP 建连、写入及连接池等待时限。 |
| `MCP_DISCOVERY_TIMEOUT_SECONDS` | `15` | 页面发现 MCP 工具的一次完整操作，包含建连、握手及分页。 |
| `MCP_CALL_TIMEOUT_SECONDS` | `120` | 一次正式 MCP 操作，包含建连、握手、工具结构复核及远端调用；各阶段共享本次时限。 |
| `KNOWLEDGE_SEARCH_TIMEOUT_SECONDS` | `60` | 一次知识检索，HTTP 检索与 Agent 检索共用。 |
| `KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS` | `60` | 一批嵌入请求，检索与索引共用，并受嵌入供应商请求时限约束。 |
| `SCRIPT_EXECUTION_TIMEOUT_SECONDS` | `30` | 一次正式脚本执行，适用于用户 Tool 和 Skill 脚本。 |
| `SCRIPT_TEST_TIMEOUT_SECONDS` | `10` | 页面上的 Tool 和 Skill 脚本试运行。 |
| `RUNTIME_RECURSION_LIMIT` | `9999` | 每次执行或审批恢复的 LangGraph 最大图步数；包括编排和中间件步骤，不等于模型或工具调用次数，也不是秒数。 |

正式调用的有效时限按调用类型确定：

- 模型：Agent/Run 保存的 `model_timeout_seconds` 控制一次调用的整体等待，供应商 `timeout_seconds` 独立控制 SDK 请求；OpenAI、兼容服务、Ollama 和 Azure 的异步流式数据块等待也显式使用该值，SSE 心跳不会延长数据块等待时限。较短的限制可能先触发。历史摘要同样有单次模型时限，不受整轮累计耗时影响。
- MCP：`min(Agent.tool_timeout_seconds, MCP_CALL_TIMEOUT_SECONDS)`；HTTP 建连另受 `MCP_CONNECT_TIMEOUT_SECONDS` 约束。初始化、结构复核和调用共享一次 MCP 预算，SDK 进度通知不延长它。工具发现使用单独的发现预算。
- 知识检索：Agent 中使用 `min(Agent.tool_timeout_seconds, KNOWLEDGE_SEARCH_TIMEOUT_SECONDS)`；独立 HTTP 检索采用部署的检索时限。嵌入请求还受 `min(嵌入供应商.timeout_seconds, KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS)` 约束，完整检索预算覆盖其内的多次嵌入和召回。
- 脚本：正式执行使用 `min(Agent.tool_timeout_seconds, SCRIPT_EXECUTION_TIMEOUT_SECONDS)`；页面试运行使用 `SCRIPT_TEST_TIMEOUT_SECONDS`。容器在超时或取消时清理；超时不会自动重放脚本或外部写入。
- Skill：纯文档 Skill 按授权的 `read_file` 工具读取，没有独立的整个 Skill 倒计时；Skill 调用的模型、MCP、脚本和知识库遵循各自的时限。

例如，为较长的审查任务配置工程默认值；已有 Agent 和供应商还需按下方对照表分别修改页面中的已保存值：

```dotenv
ARTIFACT_UPLOAD_MAX_BYTES=20971520
ATTACHMENT_CONTEXT_MAX_BYTES=20971520
AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS=1200
AGENT_MAX_MODEL_TIMEOUT_SECONDS=3600
AGENT_DEFAULT_TOOL_TIMEOUT_SECONDS=300
AGENT_MAX_TOOL_TIMEOUT_SECONDS=3600
AGENT_DEFAULT_MAX_OUTPUT_TOKENS=16384
AGENT_MAX_OUTPUT_TOKENS=65536
AGENT_MODEL_CONNECTION_MAX_RETRIES=1
AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS=1
MCP_CONNECT_TIMEOUT_SECONDS=10
MCP_DISCOVERY_TIMEOUT_SECONDS=30
MCP_CALL_TIMEOUT_SECONDS=300
KNOWLEDGE_SEARCH_TIMEOUT_SECONDS=120
KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS=90
SCRIPT_EXECUTION_TIMEOUT_SECONDS=180
SCRIPT_TEST_TIMEOUT_SECONDS=30
RUNTIME_RECURSION_LIMIT=9999
```

上述时限、容量、输出整数额度和图步数必须大于零；连接重试次数及重试等待允许为 0。三个 Agent 默认值不能超过各自可设置上限。已经保存的 Agent 参数优先，新 Run 固定为配置快照；修改环境默认值不会改写已有 Agent 或历史 Run。已有 Agent 需在编辑页保存新的模型/工具超时、输出 Token，新提交的 Run 才采用新数值。供应商请求时限在供应商页面单独调整；输出还受实际模型能力和上下文窗口约束，大文件容量不代表模型能容纳同等大小输入。

`max_model_calls`、`max_tool_calls`、`max_total_tokens` 是每轮累计预算，与上表中的单次时限、单次输出上限分别生效：

| 参数 | 创建时省略或传 `null` | 填写时的范围与用途 |
| --- | --- | --- |
| `max_model_calls` | 默认 `null`，不设置该项上限。 | `1～100`，限制本轮可开始的模型调用次数，包含历史摘要、连接重试与纠正回合。 |
| `max_tool_calls` | 默认 `null`，不设置该项上限。 | `1～100`，限制本轮工具调用次数，失败调用也计数。 |
| `max_total_tokens` | 默认 `null`，不设置该项上限。 | `1～10000000`，限制本轮上游报告的累计 Token，包含输入和输出。 |

会话任务在 Agent 创建/编辑接口配置这些预算，提交时保存到 Run 快照；独立 `POST /api/v1/runs` 可直接传入。Agent 更新时省略字段保留原值，传 `null` 清除该项上限；`0` 不是关闭上限的合法值。这三项没有对应的环境默认数值，默认 `null` 也不会关闭单次调用时限、单次输出上限或图步数限制。模型是否报告完整用量决定累计 Token 的可核算范围；本轮已超额后会停止后续调用，无法保证上游单次请求不会越过剩余额度。

模型目录中的 `context_window` 应填写当前供应商部署实际允许的窗口，不能直接照搬模型宣传上限。输入、系统提示、工具定义及预留输出共同占用窗口；例如部署窗口为 65536、完整输入已超过 32768 Token 时，再预留 32768 输出会被上游以 HTTP 400 拒绝，延长超时不能解决。未登记窗口时无法在本地可靠预检。平台将 SDK 上下文异常、明确的 `context_length_exceeded` 及兼容服务的最大上下文拒绝归为 `context_window_exceeded`，普通参数错误仍归为 `model_error`；摘要调用同样保留上下文原因码，不重发这类 400，也不保存上游错误正文。

自动摘要会把原始会话转存到检查点中的 `/conversation_history/*.md` 虚拟文件。模型可通过 `read_file` 的 `offset`、`limit` 分段回读当前线程实际存在的历史原文，补充摘要遗漏的附件事实；这些读取执行相同的运行授权、工具次数、租约和超时检查。宿主机路径、其他线程文件、嵌套路径及路径穿越不在授权范围内；既有 Skill 文件仍单独核对绑定与权限。

输出 Token 上限和调用时限需要分别判断。模型以 `length`、`max_tokens` 等结束原因停止时，平台记录 `failed/model_output_limit_exceeded`，不会把部分回答标为完成，也不会在相同额度下自动补救文件或执行被截断的工具参数；本轮已报告的模型用量仍进入 Run 和调用记录。思考模型可能在正文、工具参数生成前耗尽输出额度；应结合 Agent 的“模型思考模式”“最大输出 Token”、实际模型能力、上下文窗口和单次模型时限调整。没有明确的输出截断标记且最终正文为空时，仍记录 `empty_response`；不能仅凭输出用量等于上限判断截断。

Agent 的 `max_output_tokens` 支持显式 `null`：前端清空“最大输出 Token”后保存，或在 Agent 创建/修改请求中发送 `"max_output_tokens": null`，后续 Run 快照保留空值，支持的供应商请求不再发送 `max_tokens`、`max_completion_tokens` 或对应的输出额度参数。创建时省略字段仍使用 `AGENT_DEFAULT_MAX_OUTPUT_TOKENS`，修改时省略则保留原值；`0` 仍非法，既有整数配置和历史 Run 快照保持原值。此模式由供应商采用自身默认额度及模型硬上限，不保证自动使用模型最大输出能力；平台单次模型时限、已配置的累计 Token/费用/次数预算仍生效。Anthropic Messages 必须填写额度，配置或切换到该供应商时会返回明确提示，避免 SDK 隐式补额度。需要先执行 `uv run alembic upgrade head` 升级到 049，再重启 API 与 worker；回退 048 会将 Agent 空额度回填为旧版默认值 4096。

未指定输出额度时，上下文预检与摘要只扣除已知的输入开销，不再暗中预留平台默认输出额度；供应商自行决定的输出预留仍可能导致上下文拒绝。供应商报错继续使用脱敏故障码返回；即使请求没有指定额度，上游正常响应中的 `length`、`max_tokens` 等截断标记仍记录为 `model_output_limit_exceeded`，不会把不完整回答或工具参数当作成功，也不会以同样参数盲目重试。

Agent 编辑页 → 运行设置提供“模型思考模式”：`auto`（跟随模型，默认）、`enabled`（启用思考）、`disabled`（关闭思考）。`yp_agent_config.thinking_mode` 保存偏好，每次提交固定到 `yp_agent_run.agent_thinking_mode`；worker 使用该快照构造模型，修改 Agent 不改变已有 Run。Agent 创建/更新接口接受 `thinking_mode`，独立 `POST /api/v1/runs` 也可传同名字段，Run 响应返回 `agent_thinking_mode`；重复提交键须保持原偏好。

当前显式启闭仅适配实现 `chat_template_kwargs.enable_thinking` 的 `openai_compatible` 服务，参数通过 SDK 的 `extra_body` 写入请求体。其他供应商类型须使用 `auto`；Agent 保存、会话模型目录及提交会检查兼容性。兼容协议类型也不代表具体上游一定支持该扩展，需在目标服务验证。关闭思考不会关闭工具调用，工具仍按既有权限及自动选择规则使用；它也不能保证任意长报告都能在配置额度内生成。

`GET /api/v1/agents/runtime-limits` 向已登录用户返回附件限额、Agent 默认值、分类调用时限及图步数限制，不返回密钥、完整环境配置或整轮倒计时字段。页面读取该接口，加载失败时可以重试。知识库上传 10 MiB、生成产物 10 MiB、图片 2 MiB、脚本入参 64 KiB 及各格式解析页数/行数继续按各自限制处理。

## 模型与 Agent 配置对照及长任务实践

以下“默认值”来自当前代码和 `.env.example`，不是本机或生产环境一定采用的值。工程配置见 `app/core/config.py` 的 `BaseConfig`，页面当前可填写上限来自 `GET /api/v1/agents/runtime-limits`，已保存的具体参数以模型、供应商和 Agent 详情为准。Token、UTF-8 字节、秒数、调用次数和图步数分别计量，不能互相换算成同一项额度。

### 配置来源、覆盖顺序与生效时间

| 配置层 | 配置入口与读取规则 | 修改后的生效范围 |
| --- | --- | --- |
| 工程环境 | `FAST_API_ENV` 选择环境。常规加载的同名值优先级从高到低为：进程环境变量 → 开发环境 `.env.development.local` → `.env.<环境>` → `.env` → `ENV_FILE` 指定文件 → `BaseConfig` 代码默认值；不存在的可选文件跳过，显式指定但不存在的 `ENV_FILE` 会报错。 | API、worker 分别在进程内缓存，修改后需要两者重启并使用同一配置来源；`ENV_FILE` 在当前实现中首先装载，不是最高优先级。生产环境不加载开发本地文件。 |
| 工程默认值 `AGENT_DEFAULT_*` | 仅在创建 Agent 或独立 Run 且对应字段省略时补默认值；Agent 新建页会先填入当前默认值。 | 修改默认值不会覆盖已有 Agent，也不会改历史 Run；需要编辑已有 Agent，再提交新任务。 |
| 工程上限 `AGENT_MAX_*` | 校验页面/API 允许填写的最大整数值，三个 `AGENT_DEFAULT_*` 不能超过对应上限。 | 调高上限只放宽后续填写校验，不会自动调高 Agent 值、供应商能力或模型部署窗口。 |
| 模型与供应商页面 | 管理员在“模型管理”维护连接、部署窗口、价格及已验证能力；Agent 通过模型 ID 引用它们。 | 保存通常无需重启。连接、模型标识和部署窗口等在执行器领取时读取；已经构造的模型实例不会在调用途中动态重建。启用成本预算时的价格/币种在提交时另存快照。修改被 Agent 默认模型引用的连接关键项、模型 ID/声明能力/窗口或停用时，接口可能要求先更换 Agent 默认模型。 |
| Agent 页面 | 默认模型、生成温度、思考模式、输出额度、模型/工具时限及每轮预算保存在 Agent。 | 保存无需重启；新提交的 Run 固定这些生成参数和预算快照，修改不改变已排队、正在运行或历史 Run 中的 Agent 快照。 |
| 会话选模 | 新会话采用 Agent 默认模型；已有会话使用自身选定模型，可在空闲时切换。 | 修改 Agent 默认模型不替换已有会话模型。切换时会重新校验文本/工具能力、思考模式、窗口和成本配置。模型目录的“默认”标记也不会批量覆盖已有 Agent 或会话。 |
| 请求重试与新测试 | 同一 `request_key` 表示同一次提交；失败后重复旧键会返回原 Run。 | 要验证新配置，应等待当前运行结束或显式取消，再用新 `request_key` 提交；会话版本采用当前读取的 `expected_session_version`，Agent 修改采用当前 `expected_version`。 |

环境文件中的数值配置省略时使用上表优先级找到的值，所有来源均省略才使用代码默认。不要用 `KEY=`、`null` 或 `0` 表示通用的“无限制”：正整数配置的空字符串/`null` 会校验失败，只有字段明确允许时 `0` 才有专门含义。页面清空和 API 显式 JSON `null` 的规则按下表逐项判断；更新请求省略字段通常表示保留原值。

### 模型与供应商页面参数

| 页面字段 / API 参数 | 创建时默认、空值与范围 | 实际作用及使用建议 |
| --- | --- | --- |
| 供应商类型 `kind`、地址 `base_url` | 类型必选；地址通常必填，Gemini/Bedrock 由适配器按类型处理默认地址。 | 类型决定 SDK、认证和扩展参数路径；兼容协议不保证供应商实现了全部扩展。供应商模型 ID 需填写实际可调用的标识，显示名称可自行命名。 |
| API 密钥 / 自定义请求头 | 是否必需由供应商类型决定；编辑页留空保留已保存的凭据，API 显式 `null` 可清除凭据并重新校验。 | 使用页面保存或部署的凭据机制；页面不会回显已保存的密钥。清空输入框并保存不会自动清除原密钥。 |
| 接口版本 `api_version` / 区域 `region` | 一般可选，Azure 版本、Bedrock 区域等按供应商要求必填；缺失不是无限制。 | 填写与实际连接一致的值，避免把接口/鉴权错误当作模型或工具超时。 |
| 请求超时 `timeout_seconds` | 接口新建默认 `min(15, AGENT_MAX_MODEL_TIMEOUT_SECONDS)` 秒，页面通常初填 15；正整数，不能清空，最大值采用同一工程上限。 | 供应商 SDK 请求等待限制，与 Agent 单次调用总时限同时生效；OpenAI 系适配器的数据块等待也采用该值。长任务只调 Agent 到 1200 秒、供应商仍为 15 秒，仍可能提前中断。 |
| 声明能力 `capabilities` / 已验证能力 `verified_capabilities` | 声明默认空列表，表示待配置；声明不能传 `null`。 | 勾选能力不等于验证通过。Agent 使用已验证文本能力；绑定 Skill、Tool、知识库还要求已验证工具能力；图片等功能也要验证对应能力。整体“未全部验证”不表示每一项都不能用，查看单项 ✓。 |
| 部署上下文窗口 `context_window` | 默认 `null`，页面留空表示未知；填写时必须为正整数。 | 包含输入、系统指令、历史、工具契约/结果及输出。未知窗口不会被视为真实无限上下文，平台无法据此可靠预检并依赖框架默认摘要阈值；填大数字不会扩容供应商部署。应登记当前服务实际允许的值。 |
| 输入 / 输出价格 `input_price`、`output_price` | 默认 `null` 表示未知；`0` 是合法的零估算价格，不能用未知代替零。最多 6 位小数，单位为每百万 Token。 | 成本预算要求输入、输出价格完整；按统一单价估算，未单独处理供应商其他计费项。价格留空不会限制普通调用，但不能启用需要该价格的成本预算。 |
| 币种 `price_currency` | 默认 `USD`，三位大写代码；不能清空。 | 价格和 Agent 成本上限使用此币种，平台不进行汇率换算。估算成本不是供应商账单硬限额。 |
| 启用状态 / 模型默认标记 | 新建默认启用；模型默认标记初始为否。 | 停用会影响可选性与执行前复核；默认标记不替代 Agent 的显式默认模型配置。更改连接关键参数或模型 ID/声明能力后需重新验证能力。 |

### Agent 页面参数与空值语义

| 页面字段 / API 参数 | 创建默认与留空含义 | 工程关联、填写范围和任务影响 |
| --- | --- | --- |
| 默认模型 `default_model_id` | API 必填，页面从可用模型中选择，不能传 `null`。 | 影响新会话；已有会话选模独立。绑定 Skill、Tool 或知识库后，应选择通过工具能力验证的模型。 |
| 系统指令 `system_prompt` | 默认空字符串，允许清空；空指令不等于请求没有平台/Skill 系统内容。 | 最多 32768 字符，每次请求占用输入窗口；长任务建议明确分章节审查、依据定位、结构化字段及产物要求。 |
| 生成温度 `temperature` | 默认 `1`，范围 `0～2`，不能为空；`0` 为合法温度。 | 控制随机性，不能增加输出长度或延长时间。供应商可能有额外约束；若支持可调温度，结构化审查可从较低温度试起，以实际质量比较。 |
| 最大输出 Token `max_output_tokens` | 创建时省略采用 `AGENT_DEFAULT_MAX_OUTPUT_TOKENS=4096`；页面清空或 API 显式 `null` 表示不向供应商指定额度；修改时省略保留原值。 | 整数范围 `1～AGENT_MAX_OUTPUT_TOKENS`，代码上限默认 65536；该上限不是模型实际能力。正文、工具参数和上游报告的思考输出可能共同占额度。`null` 使用供应商默认额度，不能保证无限或自动用满最大输出。Anthropic 要求填写整数。 |
| 模型思考模式 `thinking_mode` | 默认 `auto`（跟随供应商），不能清空；`auto` 不等于关闭思考。 | `enabled/disabled` 仅支持已实现 `enable_thinking` 扩展的 OpenAI 兼容服务。长思考同时消耗时间和输出额度；关闭思考仍保留工具权限，具体质量和扩展支持需在目标服务验证。 |
| 单次模型超时 `model_timeout_seconds` | 创建时省略采用 `AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS=120` 秒；不能为空。 | 正整数，最大 `AGENT_MAX_MODEL_TIMEOUT_SECONDS`（默认 3600）；每次完整模型调用分别计时，含思考、正文、工具参数生成。摘要也受该时限控制，连接重试和其等待不重置原时限。 |
| 单次工具超时 `tool_timeout_seconds` | 创建时省略采用 `AGENT_DEFAULT_TOOL_TIMEOUT_SECONDS=120` 秒；不能为空。 | 正整数，最大 `AGENT_MAX_TOOL_TIMEOUT_SECONDS`（默认 3600）；MCP、知识检索、脚本采用该值与对应工程分类时限的较小值。纯 Skill 文档读取没有独立的整个 Skill 倒计时。 |
| 每轮模型调用上限 `max_model_calls` | 默认 `null`；留空不设置本轮该项上限，填写 `1～100`；`0` 非法。 | 不受 `AGENT_DEFAULT_*` 补值；包括生成、摘要、模型自我纠正及允许的连接重试。整轮长不等于某一次超时，但累计调用仍可能触顶。 |
| 每轮工具调用上限 `max_tool_calls` | 默认 `null`；留空不设置本轮该项上限，填写 `1～100`；`0` 非法。 | Skill 文件读取、正式工具调用和失败调用都会消耗次数；需要为读取说明、校验、修正和最终渲染预留次数。 |
| 每轮总 Token 上限 `max_total_tokens` | 默认 `null`；留空不设置本轮累计上限，填写 `1～10000000`；`0` 非法。 | 累计上游报告的输入 + 输出，包含摘要；历史在不同请求中重复发送会重复计入。与单次输出上限、模型窗口分别生效；启用后不能取得完整用量会失败，单次请求可能越过剩余额度。 |
| 每轮估算成本上限 `max_estimated_cost` | 默认 `null`；留空不设本轮该项上限，填写至少 `0.000001`、最多 6 位小数；`0` 非法。 | 依赖所选模型的完整价格和用量；按价格/币种快照累计，无法取得用量时不能可靠执行该预算。单次请求可能超出剩余额度；未启用此预算时 Run 的估算累计值不能当作完整账单。 |
| Skill / Tool / 知识库关联 | 默认空列表，表示未关联；空列表可清除关联，列表不能为 `null`。 | Skill/Tool 分别最多 16 项、知识库最多 8 项；提示词提及名称不能授予权限。关联越多，所需工具说明及上下文也可能增加；不要把缺少绑定或权限失败当作额度不足。 |

输出空额度需数据库版本至少为 049；升级后重启 API/worker。回退 048 会将 Agent 的空输出额度回填为 4096。每轮预算的 `null` 只关闭该项本地限制，模型窗口、供应商限制、单次时限、图步数、权限及资源配额继续生效。独立 `POST /api/v1/runs` 可传自身时限和每轮预算；它目前没有公开的 `max_output_tokens` 字段，不能把 Agent 编辑请求直接作为独立 Run 请求。

### 长任务中容易遗漏的工程阈值

| 工程参数 / 入口 | 代码默认值与空值规则 | 与页面参数的组合及排查重点 |
| --- | --- | --- |
| `MCP_CONNECT_TIMEOUT_SECONDS` | 10 秒，正整数，不能清空/设 0。 | MCP 建连、写入和连接池等待有独立限制；整次调用的预算较大也不能覆盖较短的建连限制。 |
| `MCP_DISCOVERY_TIMEOUT_SECONDS` | 15 秒，正整数。 | 页面发现工具时的总时限，含握手和分页；与对话中的正式调用时限独立。 |
| `MCP_CALL_TIMEOUT_SECONDS` | 120 秒，正整数。 | 正式 MCP 总预算为 `min(Agent.tool_timeout_seconds, 此值)`，含初始化、契约复核及调用；进度通知不会刷新总预算。 |
| `KNOWLEDGE_SEARCH_TIMEOUT_SECONDS` | 60 秒，正整数。 | Agent 检索预算为 `min(Agent.tool_timeout_seconds, 此值)`；独立检索接口也使用此工程值，完整检索涵盖其内的嵌入和召回。 |
| `KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS` | 60 秒，正整数。 | 单批嵌入预算还受嵌入供应商 `timeout_seconds` 限制；只调高聊天模型供应商不能解决嵌入超时。 |
| `SCRIPT_EXECUTION_TIMEOUT_SECONDS` / `SCRIPT_TEST_TIMEOUT_SECONDS` | 正式 30 秒 / 页面试运行 10 秒，均为正整数。 | 正式 Tool 和 Skill 脚本采用 `min(Agent.tool_timeout_seconds, 正式时限)`；试运行独立使用测试时限。依赖环境构建还有独立的 `SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS=180`，不是脚本运行时限。 |
| `AGENT_MODEL_CONNECTION_MAX_RETRIES` / `AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS` | 额外重试 1 次 / 等待 1 秒；范围分别 `0～3` 次、`0～30` 秒，0 分别表示禁用额外重试、立即重试。 | 仅对尚无模型数据的 OpenAI/Anthropic SDK 连接错误（含使用相应 SDK 的兼容服务）有限重试；每次尝试计入模型次数，共用原时限。超时、400/参数错误、输出截断及已开始返回数据的请求不因此重试。 |
| `RUNTIME_RECURSION_LIMIT` | 9999，正整数。 | 单次执行/审批恢复的图步数，包含中间件及编排，不等于调用次数。通常先排查重复循环，再决定是否提高。旧 `RUN_EXECUTION_TIMEOUT_SECONDS` 已不生效。 |
| `ARTIFACT_UPLOAD_MAX_BYTES` / `ATTACHMENT_CONTEXT_MAX_BYTES` | 各 20971520 字节（20 MiB），正整数。 | 分别限制对话上传单文件和本轮正文/元信息总字节；满足容量检查仍可能超过模型 Token 窗口。窗口不足时按章节/批次审查并汇总，摘要后需要细节可分段回读已授权历史。 |
| `TOOL_CALL_GLOBAL_LIMIT` / `TOOL_CALL_USER_LIMIT` / `TOOL_CALL_RUN_LIMIT` | 16 / 4 / 2，均至少 1。 | 限制同时执行的工具数量，和 `max_tool_calls` 的累计次数不同。容量不足时应检查并发、资源和租约，单纯延长超时不能增加容量。 |
| 文件存储与配额 | 所有者默认最多 1 GiB、1000 个文件，保留期 30 天；生成单文件等还有各自上限。 | 参考“文件存储”“开发环境文件盘点与手动清理”。长任务完成文字审查后仍需有容量生成/发布文件；开发测试应定期盘点，不要把渲染或配额错误统称模型超时。 |
| 独立 Run 的 `max_queue_wait_seconds` | 默认 `null` 不设排队截止；填写 `1～86400` 秒，`0` 非法。Agent 会话页面未暴露此项。 | 只约束领取前的排队等待，不控制领取后的任务总时长；可用于避免无 worker 或拥堵时无限排队。 |
| worker / 代理 / 浏览器 | `GET /api/v1/health/worker` 检查 worker；反向代理/模型服务的时限由其部署决定。 | 无 worker 会排队，重启运行中的 worker 可能中断任务；心跳和租约用于判断存活，不是任务总时限。前端断线、刷新或某个预览请求被取消不等于 Run 已取消，应查询 Run 状态并恢复事件流。 |

### 按场景选择配置起点

以下数值是本项目的调试起点，不是保证完成时间或模型能力声明；先登记真实部署窗口，测量首包等待、模型整次耗时、工具耗时与上游用量，再为正常波动留出余量。调整工程默认值后，务必同步检查已有 Agent 和供应商页面值。不要为了测试一项参数而直接放大所有限制。

| 实际场景 | 模型与供应商页面 | 工具与工程分类时限 | 预算、上下文和流程 |
| --- | --- | --- | --- |
| 普通短问答 | Agent 单次模型 60～120 秒；供应商请求等待可从相近范围验证；输出 2048～4096 Token。 | 无工具可保留默认；有工具根据各自耗时设置。 | 可用较小且明确的调用/费用预算；思考模式先保持已验证的配置。 |
| 长文施工方案审查、结构化校验和报告 | Agent 单次模型 600～1200 秒；供应商请求超时同步检查，可从 600～1200 秒验证；输出从模型可支持的 16384 开始评估，或清空观察供应商默认行为。 | Agent 工具 300 秒；正式脚本可从 180 秒、MCP 300 秒、检索 120 秒/嵌入 90 秒验证，仅修改实际用到的分类；试运行时限另设。 | 开发定位阶段可清空每轮预算，观察真实调用/用量后再设置；生产用实测值加余量。保留读取 Skill、校验修正、最终渲染和回复所需次数。 |
| 思考模型首包慢或持续推理 | Agent 单次模型可从 1200～1800 秒验证，供应商等待同步覆盖预期首包/数据块等待。 | 模型思考耗时不需要同步放大脚本执行时限。 | 思考消耗输出额度；长时间仅有思考时，应比较受支持的关闭思考配置、缩小单次审查范围或切换已验证模型，不能保证调大额度会结束思考。 |
| 多轮、多步骤、历史较长 | 每次调用时限按单次耗时设置，整轮可以明显长于它。 | 分类时限按每次操作设置，避免把整轮分钟数直接填到每个工具。 | 为摘要、重复输入和纠正预留预算；示例：10 次调用每次输入约 40000、输出约 10000，累计约 500000 Token，另加摘要/重试，预算填 100000 会过早耗尽。 |
| 长时间远程 MCP 或大批检索 | 未变慢的聊天模型参数保持原值；单独检查嵌入供应商。 | 同步放宽 Agent 工具与实际分类时限，覆盖建连、握手、嵌入等内部阶段；超出默认工程可填上限时先调整相应 `AGENT_MAX_*`。 | 控制并发、分页和每批规模；远程服务本身的限额仍需核对。长耗时操作返回结果后才进入下一次模型调用。 |
| 文件输入接近部署窗口 | 填写实际 `context_window`，为输入和输出共同留空间。 | 上传容量按文件需要配置；解析/检索耗时分别评估。 | 示例：窗口 65536、指定输出 16384 时，当前固定开销 512 后的输入预算约 48640 Token，包含消息、系统指令和工具；摘要触发约为输入预算的 75%。估算仍非上游保证。清空输出额度时按登记窗口的四分之一预留生成空间，再扣固定开销；完整请求与摘要阈值共同遵守该输入预算。该预留不会向供应商发送固定输出上限，实际思考/正文仍可能触及供应商限制。 |

长文审查的 Agent 字段示例（在 Agent 编辑接口提交，修改时另附当前 `expected_version`；供应商页面还需检查 `timeout_seconds`，不会由此自动修改）：

```json
{
  "model_timeout_seconds": 1200,
  "tool_timeout_seconds": 300,
  "max_output_tokens": null,
  "thinking_mode": "auto",
  "max_model_calls": null,
  "max_tool_calls": null,
  "max_total_tokens": null,
  "max_estimated_cost": null
}
```

该示例采用供应商默认输出额度且清除了四项每轮预算，适合定位实际用量；正式运行的费用预算应结合业务要求和实测值设置。单次模型时限和部署分类限制继续生效。正式复测建议新建会话、使用同一份输入和模型对比；记录 `run_id`、`failure_code`、调用时间线及已报告用量，避免同时改变多项参数后无法确定改善来源。

### 按故障原因调整配置

| 状态 / 故障码或现象 | 先检查什么 | 对应处理 |
| --- | --- | --- |
| `timed_out / model_execution_timeout` | Agent/Run 的单次模型时限、最后一次模型调用和思考耗时。 | 有正常进展但预算不足时调大 Agent 单次时限；若一直思考/重复循环，缩小任务或比较模型/思考配置。 |
| `timed_out / provider_timeout` | 供应商请求/数据块等待时限，以及模型服务/网络/代理等待。 | 同步调整实际供应商或上游部署；只调 Agent 不足以覆盖较短的供应商限制。 |
| `timed_out / tool_execution_timeout` | 实际工具类型、Agent 工具时限、分类时限和内部阶段。 | 按上述 `min(...)` 规则调整两层，区分正式执行、页面试运行、发现和依赖构建。 |
| `failed / model_output_limit_exceeded` | 单次输出额度、实际供应商默认额度、思考输出及截断标记。 | 在真实窗口和模型能力允许时增加额度、清空交由供应商决定，或分批/改变思考模式；延长超时不增加 Token 额度。 |
| `failed / context_window_exceeded` | 实际部署窗口、输入/工具/历史长度与输出预留。 | 登记正确窗口、减少单次输入和工具上下文、按批审查；不能通过把目录窗口填写得比部署更大来修复。 |
| `failed / model_call_budget_exceeded`、`tool_call_budget_exceeded` | Run 中的每轮次数快照、摘要/失败/纠正调用是否计入。 | 在新 Run 中提高对应次数或清空；相同参数的模型自我纠正并不是固定重试次数配置。 |
| `failed / token_budget_exceeded`、`cost_budget_exceeded` | 已报告累计用量、重复输入、价格/币种与每轮预算。 | 核实计量后提高或清空对应预算，缩小每轮任务；单次请求可能越过剩余额度。 |
| `failed / token_usage_unavailable`、`cost_usage_unavailable` | 已开启的累计预算及供应商是否返回完整用量。 | 选择/修正能报告所需用量的供应商，或在接受失去该预算保护的前提下清空对应预算；增加数值不能补齐缺失用量。 |
| `failed / model_error`、`empty_response`、`summary_error` | 调用记录、供应商接口/能力、脱敏日志及对应模型/摘要阶段。 | 区分参数/认证/连接/格式/空正文问题，不能统一视为额度低；输出截断另有明确故障码。 |
| `failed / graph_step_limit_exceeded`、`tool_capacity_exceeded` | 重复调用、图步数及工具并发/租约。 | 分别处理循环或容量；图步数和工具累计次数是不同限制。 |
| 校验工具失败或 `artifact_required` | 工具结果中的字段错误、脚本入参、报告模板、产物类型及文件配额。 | 修正输入/Skill 契约或产物流程；模型修正并再次调用会计次数，没有统一的“工具失败固定重试三次”。 |
| `lease_expired`、`lease_monitor_error` 或长时间排队 | worker 健康、进程日志、数据库连接/锁及租约。 | 恢复执行环境后按状态重新提交；避免在任务执行中重启 worker，不通过单纯延长模型超时修复。 |
| 预览请求 `(canceled)`、SSE 断开或刷新页面 | Run 是否仍为 `running`、是否出现终态事件。 | 先查询状态并恢复消息/事件读取，不因前端单次请求结束而重复创建任务；需要停止任务时使用显式取消。 |
| `timed_out / queue_timeout`（独立 Run） | 排队截止设置、worker 是否在线及排队积压。 | 调整独立 Run 的排队预算或恢复 worker；模型单次时限在领取后才与模型调用相关。 |
| `execution_timeout` 或旧整轮超时记录 | 是否为历史记录、内部执行调用方显式传入的时限。 | 当前默认对话无整轮总倒计时，旧 `RUN_EXECUTION_TIMEOUT_SECONDS` 不控制新对话；不要据此反复增加无效环境变量。 |

当前可直接核对的实现位置：`app/core/config.py`、`app/schemas/agent.py`、`app/schemas/provider.py`、`app/schemas/runtime_limits.py`、`app/services/call_timeouts.py`、`app/services/model_adapter.py`、`app/services/session_models.py`、`app/services/run_manager.py` 与 `app/services/run_executor.py`。本文中的建议用于选择可解释的配置，不保证供应商、网络、工具契约或模型生成必定成功。
