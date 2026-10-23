# DeepAgents 运行时兼容性记录

## 验证版本

Python 3.12；依赖以 `uv.lock` 为准。本轮实测：

| 包 | 版本 |
| --- | --- |
| deepagents | 0.7.29 |
| langchain | 1.4.6 |
| langchain-core | 1.6.12 |
| langgraph | 1.2.16 |
| langgraph-checkpoint | 4.3.0 |
| langgraph-checkpoint-sqlite | 3.1.2 |
| langchain-openai | 1.7.2 |
| langchain-anthropic | 1.7.6 |

运行 `uv run pytest -q tests/test_deepagents_spike.py` 可重复执行本地验证。程序使用 `FakeMessagesListChatModel`，不需要供应商凭据，也不会向外部模型发送请求。

## 已验证的 API

| 能力 | 实测结果 |
| --- | --- |
| Agent 初始化与模型回合 | `create_deep_agent(model=...)` 可接受 `BaseChatModel`；假模型完成工具前后的两个回合。 |
| 工具调用 | `tools=[echo]` 注册工具；模型产生 `tool_calls` 后，状态中保存 `ToolMessage`。 |
| 流式事件 | `stream_mode=["updates", "messages"]` 返回 `(mode, payload)`；观察到 `model`、`tools` 节点更新及 AI/工具消息。 |
| Skill 装载 | `skills=["/skills/"]` 配合 `StateBackend` 的 `files` 输入读取 `SKILL.md`；`skills_metadata` 可从检查点状态读取，不在 `invoke()` 的对外返回值中。 |
| 检查点 | `InMemorySaver` 按 `configurable.thread_id` 保存消息和 `checkpoint_id`；M3 另用 SQLite Checkpointer 验证独立进程重启后可恢复同一线程。 |
| 审批与恢复 | `interrupt_on={"echo": True}` 在工具执行前中断；用 `Command(resume={"decisions": [{"type": "approve"}]})` 恢复后才执行工具。 |
| 取消 | 取消消费 `astream()` 的 asyncio 任务会把取消传入正在等待的异步工具；工具的清理块得到执行。 |

## 真实模型验证（M2）

使用本地已配置的供应商连接 `id=1` 和模型 `id=1`，验证了一次 OpenAI 兼容供应商的最小文本调用、流式文本输出及工具调用。流式输出和工具调用均返回输入/输出 Token 用量；测试未记录凭据或生成正文。该验证覆盖 LangChain 模型适配层，尚未验证真实模型在 `create_deep_agent()` 中的完整运行、审批和持久化恢复。

同一模型的最小文本调用再次实测成功，返回 13 个输入 Token 和 1 个输出 Token；以错误密钥调用同一上游时返回 `authentication`。超时、限流与网络异常目前由模拟上游验证分类，尚未在真实上游复现。

供应商 `id=1` 的 `/models` 连接测试在真实上游返回 HTTP 200、1 个模型；使用错误密钥时返回 HTTP 401 并归类为 `authentication`。验证记录只保留状态码、数量和故障类别，不包含模型列表或响应正文。

`build_chat_model()` 根据已启用的供应商与模型构造 `ChatOpenAI`、`ChatAnthropic`、`AzureChatOpenAI`、`ChatGoogleGenerativeAI` 或 `ChatBedrockConverse`，传入所需的凭据、地址、区域、版本和超时；Ollama 使用无需密钥的 OpenAI 兼容接口。模型适配器自身关闭自动重试；管理员探测服务对 HTTP `502/503/504` 最多重试一次，流式输出开始后不重试。官方 OpenAI/Anthropic 与 Azure/Gemini/Bedrock 目前通过模拟协议和配置测试，尚无真实云凭据调用结果。管理员可逐项探测模型的文本、流式、工具、固定结构化输出和视觉能力；`openai` 类型还可使用 Responses API 探测公开推理摘要，其他类型标记为不支持。摘要探测仅检查已完成响应中的 `reasoning.summary[].summary_text`，不读取或保存隐藏推理字段；该协议目前仅有模拟上游测试，尚待官方真实模型验收。

此前使用连接/模型 `id=1` 调用逐项探测：流式和工具均成功并返回 Token 用量；文本首次遇到上游 HTTP 502，人工重试成功。当前新增自动重试策略已由模拟上游验证，尚未再次触发真实上游 502。最终失败会撤销该能力的当前验证状态，并在调用记录中保留脱敏故障类别。上述调用未保存响应正文或凭据，也未验证完整 DeepAgents 编排。

同一真实模型的固定结构化输出探测成功，返回输入/输出 Token 用量；解析结果只用于判定是否符合 `{"value":"ping"}`，未保存原始消息或解析内容。该结果证明当前模型在函数调用模式下可产生这一结构，不代表任意复杂 Schema 都可用。官方 OpenAI 和 Anthropic 的结构化调用仍待真实凭据验证。

视觉探测发送内嵌的 64×64 四色象限 PNG，并检查回答中颜色的顺序。模型 `id=1` 在 128 输出 Token 上限下曾以 `finish_reason=length` 结束且没有可见回答；视觉请求上限调整为 512 后探测成功并返回用量。这个结果只覆盖固定图像识别，不推断 OCR、复杂图表或其他多模态任务的质量。

本机 Ollama 的 OpenAI 兼容地址 `http://127.0.0.1:11434/v1` 实测 `/models` 返回 HTTP 200 和 4 个模型。以 `deepseek-r1:1.5b` 进行最小文本测试、LangChain 适配调用、文本及流式能力探测均成功并返回 Token 用量；没有在业务库创建测试连接，也没有记录回答正文。该结果仅验证这一模型在上述调用中的行为，工具、结构化输出和视觉能力未测。

随后本机 Ollama 模型列表新增 `gemma4:31b-cloud`，共返回 5 个模型。该模型的最小文本、文本、流式、工具、固定结构化输出和四色视觉探测均成功并返回 Token 用量；错误模型 ID 返回 HTTP 404 并归类为 `model_not_found`。公开推理摘要按 Ollama 类型返回 `unsupported_capability`，未读取模型的隐藏推理内容。可运行 `YPOINT_TEST_OLLAMA_GEMMA4=1 uv run pytest -q tests/test_capability_probe.py::test_live_ollama_gemma4` 重新验证；此测试不写入业务库。

| 供应商类型 | 模型列表 | 最小文本测试 | 运行时适配 | 真实验证 |
| --- | --- | --- | --- | --- |
| OpenAI 兼容接口 | `/models`，缺失时手动录入 | OpenAI 兼容聊天接口 | `ChatOpenAI` | 连接/模型 `id=1` 的文本、流式、工具、用量及错误密钥分类 |
| OpenAI 官方 | `/models` | OpenAI 聊天接口 | `ChatOpenAI` | 仅模拟协议与配置；待真实凭据 |
| Anthropic | `/models`，缺失时手动录入 | Messages 接口 | `ChatAnthropic` | 仅模拟协议与配置；待真实凭据 |
| Ollama | OpenAI 兼容 `/models` | OpenAI 兼容聊天接口 | `ChatOpenAI` | 本机 `deepseek-r1:1.5b` 的文本/流式；`gemma4:31b-cloud` 的文本、流式、工具、结构化、视觉及用量 |
| Azure OpenAI | 数据面不列部署，手动录入部署名 | 部署路径 + API 版本 | `AzureChatOpenAI` | 模拟协议与配置；待真实资源验收 |
| Gemini Developer API | 分页读取支持 `generateContent` 的模型 | `generateContent` | `ChatGoogleGenerativeAI` | 模拟协议与配置；待真实 API Key 验收 |
| AWS Bedrock | 区域内文本基础模型；推理配置文件手动录入 | Converse API | `ChatBedrockConverse` | 模拟 SDK 与 AWS 凭据故障；待真实区域验收 |

PostgreSQL 的 M2 迁移与关键流程按本轮约定延期，当前数据库验收仅覆盖 SQLite 和独立 MySQL 测试库。

## M3 运行时验证

`open_sqlite_checkpointer()` 和 `open_async_sqlite_checkpointer()` 接受独立的 SQLite 文件路径，并管理 LangGraph Checkpointer 的连接生命周期。假模型在一个进程中产生待审批工具调用，第二个进程批准并继续执行，第三个进程追加流式对话，第四个进程重新读取同一线程；检查点 ID 随运行推进，工具结果和最终消息均可恢复。异步 Checkpointer 另验证关闭并重开连接后的审批恢复。内存数据库被拒绝，因为它不能提供重启恢复。

本机 Ollama `gemma4:31b-cloud` 通过 `build_chat_model()` 进入 `create_deep_agent()`，真实验证工具调用、`messages`/`updates` 流式事件和模型 Token 用量。该测试用内存检查点，需显式设置 `YPOINT_TEST_OLLAMA_M3=1`，不创建业务库记录。SQLite 检查点测试用假模型，因此目前未验证真实模型与持久化检查点组合运行。

SQLite Checkpointer 保存完整图状态，可能包含消息与工具结果；测试使用临时文件。生产环境的数据库适配、访问控制、加密、保留清理、崩溃恢复、并发和取消仍待设计及验证。

M3 已增加独立的 Run/Event 业务表：Run 的 `thread_id` 可供同一用户后续运行复用，最近 `checkpoint_id` 可指向 LangGraph 检查点；状态存储和游标回放已验证。检查点失败补偿、内容事件和 SSE 仍待验证。

后续 M3 增量已把 Run、可见消息和 SQLite Checkpointer 接入内部文本执行器：用户输入随排队运行提交，执行器领取后解析仍启用的用户/模型/供应商配置，调用 DeepAgents，并在完成事务中保存最终回答与检查点 ID。取消请求、用户停用和超时会中断模型任务；模型原始异常不会写入 Run Event。本机 Ollama `gemma4:31b-cloud` 的数据库队列扫描到最终回答入库已通过显式真实测试。检查点和业务表仍是两个独立存储，无法原子提交；若检查点已写入而业务事务失败，当前策略依赖租约过期后标记失败，不自动重复执行外部副作用。该链路尚未提供对外 API 或生产级恢复保证。

## 接入限制

- `FakeMessagesListChatModel.bind_tools()` 在验证程序中只返回自身；该程序验证编排流程。真实兼容模型的工具协议、流式输出和 Token 用量已单独验证，但未覆盖完整 Agent 编排与故障场景。
- SQLite Checkpointer 已验证独立进程重启后继续审批与对话；生产存储映射及执行器崩溃处理仍属于 M3，不能从本地测试推断已支持。
- 默认 Agent 包含文件工具、`execute` 和子 Agent 工具；本轮默认 `StateBackend` 不提供可执行沙箱。正式接入前必须由平台限制工具发现、资源访问和执行权限。
- 取消测试仅证明 asyncio 取消传递。持久化 Run 状态、跨进程取消、超时竞态及副作用幂等仍属于 M3。
- Skill 测试只验证说明文件被加载；导入校验、版本固定和用户授权属于 M5。
