# 架构与核心业务

了解主要模块、对象关系和数据边界。

[文档总览](README.md) · [项目首页](../README.md)

## 核心业务概览

管理员登录后可配置供应商和模型。登录用户可创建自己的 Agent、Skill、Tool 和知识库，或使用被指定共享的 Agent 创建独立会话进行多轮对话。对话统一经 AG-UI 入口提交，创建持久化 Run，由独立 Worker 调用 DeepAgents，保存检查点、最终消息和可回放的公开事件。会话、附件、产物和运行按实际使用者隔离。工作台直接消费提交返回的 SSE；刷新或断线后通过快照与协议线游标恢复，最终回答从标准公开历史读取。

| 业务环节 | 当前能力 |
| --- | --- |
| 身份与模型 | 用户、会话和资源授权；供应商名称唯一、凭据加密及冲突回滚；模型接入、能力探测、用量及估算成本记录。 |
| Agent 与对话 | Agent 配置版本、模型思考偏好、知识回答模式（智能融合/仅知识库）、可空输出额度和运行快照；多会话隔离、多轮检查点续聊、会话内模型切换、指定用户共享与撤权；长会话摘要、窗口预算和原文分页回读。 |
| Skill | 标准文件包工作区，支持草稿/校验/发布、参考资料、完整 ZIP、多个模板独立契约、受管脚本沙箱及 Agent/Run 版本固定；报告 Skill ID、模板名称和字段错误支持在原运行预算内修正，候选仅来自本轮有权限的固定绑定。 |
| Tool | 私有或公开的 Python/Bash 脚本及 MCP 工具；MCP 连接支持公网、内网及本机地址的 Streamable HTTP 与固定镜像中的受控 stdio，可由多个 Tool 复用并固定连接版本；Agent 可直接关联多个 Tool，无需 Skill；Python 依赖按版本准备、锁定并支持离线 wheel，脚本在 Docker 沙箱试运行后发布，调用时复核用户授权并记录脱敏状态。 |
| Tool 治理 | MCP 凭据轮换、清除与撤销；传输及发现限额；跨进程调用与依赖准备名额；外部写幂等契约验收及结果不明禁止重放；资源核对和存储预算。 |
| 知识库 | 私有或指定只读共享；TXT/Markdown/DOCX 按文本结构处理，XLSX/CSV 保留完整记录和单元格来源，PDF 支持按页提取、本地 OCR、质量检查及原页核对；HTML 提取静态主文，JSON/YAML 保留配置路径与字段类型，PPTX 区分正文、表格及备注；可独立配置文本模型生成检索解释；后台索引、关键词/向量混合检索、发布代加工预览及固定引用，支持撤权、删除、取消与恢复。 |
| Run 执行 | 数据库队列、幂等提交、租约与心跳、取消/超时/失败终态、审批基础协议、单次模型/工具时限及图步数预算；模型响应前连接失败有限重试、上下文与输出截断分类、工具并发排队和参数同轮纠正；worker 故障后按条件恢复已完成的无工具运行。 |
| 文件与产物 | 私有上传和授权下载；TXT、PDF、DOCX、XLSX、PPTX 等附件上下文及图片输入；模型生成常见格式文件、会话内按需读取旧产物并记录来源关系；开发环境管理员可按 Agent/会话盘点本人文件，并按名称批量清理本地正文。 |
| 对话过程与统计 | 模型公开思考独立流式发布；Tool、Skill、MCP 等调用分类、耗时与脱敏失败原因；每轮及整个会话的耗时、累计已报告 Token，附件与产物大小。 |
| 实时与恢复 | 持久化事件、SSE、快照和游标补发；失败、超时或取消后保留已公开的未完成正文；审计可证明安全的只读调用（含完整结束的知识检索）及无产物失败报告支持继续对话；助手消息关联产物，上传附件保留在用户消息中。 |

核心业务闭环：用户通过 `POST /api/v1/agents/{agent_id}/ag-ui` 携带 `forwardedProps.expected_session_version` 仅提交新用户消息；省略版本或传 `null` 时须提交完整公开历史及新用户消息。业务事务保存输入、排队状态和首个事件，Worker 领取后按模型配置执行并持续写入事件。完成时最终消息、运行内生成文件与交付屏障同事务提交，协议投影再输出成功终态；工作台通过标准 POST 流展示过程，并从 `/ag-ui/threads/{session_id}` 同步公开历史及交付信息。失败历史保留最后模型片段为未完成正文；下一轮在安全条件满足时从已确认检查点或干净起点继续。上传文件正文留在私有存储，访问时重新校验所有权和有效期；Skill 文本包另存于业务数据库。

当前使用说明见[文档总览](README.md)，运行时适配边界见[DeepAgents 兼容说明](deepagents-compatibility.md)。

## 模块与数据边界

| 模块 | 职责 |
| --- | --- |
| `app/api/v1`、`app/schemas` | HTTP 路由、Bearer 授权、请求校验、公开响应和 SSE 协议。 |
| `app/api/v1/ag_ui.py` | 对话提交、审批恢复、能力查询与会话公开历史的 HTTP 入口。 |
| `app/api/v1/agent_sessions.py` | 会话创建/编辑/删除、模型切换、附件检查、Run 列表、统计与会话审批策略；Run 创建函数供 AG-UI 内部复用，不再注册旧会话 Run 提交或消息历史接口。 |
| `app/api/v1/runs.py` | 按 Run 读取状态、单轮消息、审批和调用记录，提供取消、协议快照、流票据及观察流；保留为运行资源与控制模块，不承担新对话提交或审批决定。 |
| `app/services/model_adapter.py` | 将已启用的供应商与模型配置转换为 LangChain 聊天模型。 |
| `app/services/run_manager.py` | Run 状态、幂等键、租约、消息和单调事件序号的业务事务。 |
| `app/services/agent_access.py` | Agent 所有者与指定用户授权检查，供会话、Run、SSE 和 worker 复用。 |
| `app/services/run_dispatcher.py`、`run_executor.py` | 扫描排队任务，校验用户和模型，执行 DeepAgents、工具授权、取消与预算。 |
| `app/services/runtime_adapter.py` | 配置 DeepAgents 工具、检查点线程和长会话摘要；摘要调用计入 Run 预算。 |
| `app/services/runtime_checkpoint.py` | 打开独立的 LangGraph SQLite 或 PostgreSQL Checkpointer。 |
| `app/services/artifact_store.py`、`message_attachments.py`、`attachment_text.py` | 私有文件存储、消息附件引用、常见文件文本提取和清理。 |
| `app/services/artifact_renderers.py`、`worker_tools.py` | 生成常见格式文件，按会话授权列出或读取旧产物，并装配本轮已授权用户 Tool。 |
| `app/services/user_tools.py`、`tool_environments.py` | Tool 定义、授权、版本快照、自然名称映射，以及依赖准备任务、锁和缓存校验。 |
| `app/services/script_sandbox.py`、`mcp_tools.py`、`mcp_stdio.py` | 在 Docker 中执行脚本；通过标准 HTTP 或容器 stdio 发现和调用 MCP 工具，统一处理协议与结果限额。 |
| `app/services/mcp_connections.py`、`mcp_policy.py`、`session_approval.py` | 管理 MCP 连接、固定执行配置、凭据与撤权；计算分层审批及会话放行，Tool 与 Run 继续固定版本。 |
| `app/services/knowledge.py`、`knowledge_jobs.py` | 知识库授权、完整索引代、检索审计，以及可恢复的后台解析和原子发布。 |
| `app/services/knowledge_parser.py`、`knowledge_backend.py`、`embedding_adapter.py` | 文档结构与偏移、原生全文/向量后端、独立嵌入协议和维度验证。 |
| `ypoint_cli` | `yp` 命令实现；开发启动、部署初始化、Compose 转发与部署检查。入口与参数见[统一 CLI](cli.md)。 |
| `app/services/mcp_tool_search.py`、`mcp_sessions.py`、`mcp_results.py` | 在固定授权集合内搜索并加载 Schema，管理 Run 内协议会话及大结果私有快照。 |
| `app/core/enums.py` | 跨模块业务类型、状态、角色、可见性和策略的统一枚举。 |
| `app/models`、`app/db`、`migrations/versions` | 业务表、数据库连接及 Alembic 迁移。 |

业务库保存用户、Agent、会话、Run、可见消息、事件和文件元数据。Checkpointer 按 `thread_id` 保存 LangGraph 状态，可能包含历史消息和工具结果；它与业务库不是同一个事务；可见消息只能重建受限的文字上下文，不能还原完整执行状态。文件正文在私有本地目录或 S3，不放入消息表或 SSE。进程心跳与 Run 租约心跳分别记录；反向代理和进程管理器应分别监测 API 与 worker。

### 核心业务关系

| 对象 | 关系与用途 |
| --- | --- |
| Agent、共享、会话 | Agent 归创建者所有；`yp_agent_share` 只授权指定用户使用配置。被授权用户创建并拥有自己的会话、Run 和文件，不继承 Agent 所有者的会话或文件。新会话继承 Agent 默认模型，之后独立保存当前模型；显式切换时更换 `thread_id` 并携带已完成文字历史。当前线程尚未完成过回答且其中历史 Run 均已终结、未调用工具时，下一轮也可更换线程恢复；旧 Run 与检查点保留。 |
| Run、消息 | 每次发送创建一个 Run 和一条用户消息；Run 保存提交时的 Agent 配置快照、状态和预算。完成后追加助手消息。AG-UI 标准公开历史按协议消息 ID 提供正文，`messageDetails` 提供文件、模型和运行元数据；单轮消息查询仅用于指定 Run 的辅助读取。 |
| 事件、检查点 | Run 事件按 Run 内序号记录排队、运行、文本增量、工具和终态，供快照与 SSE 恢复；Checkpointer 保存模型内部状态。事件流不替代最终消息。 |
| 上传附件 | 上传文件先写入 `yp_artifact` 和私有存储；提交 Run 时，用户消息通过 `yp_agent_message_attachment` 保存有序引用及是否进入模型上下文。前端从用户消息的 `attachments` 展示原文件。 |
| 生成产物 | 模型调用工具后，文件以 `pending` 暂存并关联 Run；Run 完成时发布为 `ready`，写入 `artifact_published`。前端从对应助手消息的 `artifacts` 展示预览或下载入口，文件内容通过独立授权下载接口获取。`context_artifact_ids` 列出本轮提供的上传参考文件，`read_source_artifact_ids` 列出生成前工具成功读取的旧产物。 |

页面恢复从 `/agents/{agent_id}/ag-ui/threads/{session_id}` 读取公开 `messages` 与 `messageDetails`，处理过程按 AG-UI 分页快照和 SSE 协议线游标追平；新提交直接消费 POST 流。文件引用的 `available` 只表示当前元数据状态，预览和下载仍逐次检查所有权及期限。

前端在用户消息的 `attachments` 展示上传文件，在助手消息的 `artifacts` 展示生成文件；两者的 `artifact_id` 都可用于授权下载。`included_in_context` 表示该上传文件是否在本轮交给模型。`context_artifact_ids` 是本轮提供的上传文件 ID；`read_source_artifact_ids` 是生成前由 `read_generated_artifact` 成功读取的同会话旧产物 ID，按首次读取顺序保存到 `yp_artifact_source`，源文件删除或过期后仍保留历史关系。产物详情与列表也返回这两个数组。读取记录不证明模型实际采用了哪些文件、段落或数据，也不等同于已有单值 `source_artifact_id` 的显式来源声明；逐项来源核对仍需要更细的引用证据。
