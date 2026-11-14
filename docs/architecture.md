# 架构与核心业务

了解主要模块、对象关系和数据边界。

[文档总览](README.md) · [项目首页](../README.md)

## 核心业务概览

管理员登录后可配置供应商和模型。登录用户可创建自己的 Agent、Skill、Tool 和知识库，或使用被指定共享的 Agent 创建独立会话进行多轮对话，也可直接提交不关联 Agent 的文本 Run。两种入口都创建持久化 Run，由独立 worker 调用 DeepAgents，保存检查点、最终消息和可重放事件。会话、附件、产物和运行按实际使用者隔离。浏览器通过快照与 SSE 游标恢复运行进度，最终回答以消息接口为准。

| 业务环节 | 当前能力 |
| --- | --- |
| 身份与模型 | 用户、会话和资源授权；供应商名称唯一、凭据加密及冲突回滚；模型接入、能力探测、用量及估算成本记录。 |
| Agent 与对话 | Agent 配置版本、模型思考偏好、知识回答模式（智能融合/仅知识库）、可空输出额度和运行快照；多会话隔离、多轮检查点续聊、会话内模型切换、指定用户共享与撤权；无需 Agent 的独立文本 Run；长会话摘要、窗口预算和原文分页回读。 |
| Skill | 标准文件包工作区，支持草稿/校验/发布、参考资料、完整 ZIP、多个模板独立契约、受管脚本沙箱及 Agent/Run 版本固定；报告 Skill ID、模板名称和字段错误支持在原运行预算内修正，候选仅来自本轮有权限的固定绑定。 |
| Tool | 私有或公开的 Python/Bash 脚本及 MCP 工具；Agent 可直接关联多个 Tool，无需 Skill；Python 依赖按版本准备、锁定并支持离线 wheel，脚本在 Docker 沙箱试运行后发布，调用时复核用户授权并记录脱敏状态。 |
| Tool 治理 | MCP 凭据轮换、清除与撤销；传输及发现限额；跨进程调用与依赖准备名额；外部写幂等契约验收及结果不明禁止重放；资源核对和存储预算。 |
| 知识库 | 私有或指定只读共享；TXT/Markdown/DOCX 按文本结构处理，XLSX/CSV 保留完整记录和单元格来源，PDF 支持按页提取、本地 OCR、质量检查及原页核对；HTML 提取静态主文，JSON/YAML 保留配置路径与字段类型，PPTX 区分正文、表格及备注；可独立配置文本模型生成检索解释；后台索引、关键词/向量混合检索、发布代加工预览及固定引用，支持撤权、删除、取消与恢复。 |
| Run 执行 | 数据库队列、幂等提交、租约与心跳、取消/超时/失败终态、审批基础协议、单次模型/工具时限及图步数预算；模型响应前连接失败有限重试、上下文与输出截断分类、工具并发排队和参数同轮纠正；worker 故障后按条件恢复已完成的无工具运行。 |
| 文件与产物 | 私有上传和授权下载；TXT、PDF、DOCX、XLSX 等附件上下文及图片输入；模型生成常见格式文件、会话内按需读取旧产物并记录来源关系；开发环境管理员可按 Agent/会话盘点本人文件，并按名称批量清理本地正文。 |
| 对话过程与统计 | 模型公开思考独立流式发布；Tool、Skill、MCP 等调用分类、耗时与脱敏失败原因；每轮及整个会话的耗时、累计已报告 Token，附件与产物大小。 |
| 实时与恢复 | 持久化事件、SSE、快照和游标补发；失败、超时或取消后保留已公开的未完成正文；审计可证明安全的只读调用（含完整结束的知识检索）及无产物失败报告支持继续对话；助手消息关联产物，上传附件保留在用户消息中。 |

核心业务闭环：认证与资源授权后，用户通过 Agent 会话或独立文本入口提交 Run；业务事务保存输入、排队状态和首个事件，worker 领取后按模型配置执行并持续写入事件。完成时保存最终消息和检查点引用，生成文件与助手消息关联；前端按事件游标追平过程，再从消息接口读取最终结果。会话失败后，历史接口从公开事件恢复未完成正文并保留失败状态；下一轮在安全条件满足时从已确认检查点或干净起点继续。上传文件正文留在私有存储，下载时重新校验所有权和有效期；Skill 文本包另存于业务数据库。

当前使用说明见[文档总览](README.md)，特定环境的验收范围见[历史验证记录](README.md#历史验证与研究)。

## 模块与数据边界

| 模块 | 职责 |
| --- | --- |
| `app/api/v1`、`app/schemas` | HTTP 路由、Bearer 授权、请求校验、公开响应和 SSE 协议。 |
| `app/services/model_adapter.py` | 将已启用的供应商与模型配置转换为 LangChain 聊天模型。 |
| `app/services/run_manager.py` | Run 状态、幂等键、租约、消息和单调事件序号的业务事务。 |
| `app/services/agent_access.py` | Agent 所有者与指定用户授权检查，供会话、Run、SSE 和 worker 复用。 |
| `app/services/run_dispatcher.py`、`run_executor.py` | 扫描排队任务，校验用户和模型，执行 DeepAgents、工具授权、取消与预算。 |
| `app/services/runtime_adapter.py` | 配置 DeepAgents 工具、检查点线程和长会话摘要；摘要调用计入 Run 预算。 |
| `app/services/runtime_checkpoint.py` | 打开独立的 LangGraph SQLite 或 PostgreSQL Checkpointer。 |
| `app/services/artifact_store.py`、`message_attachments.py`、`attachment_text.py` | 私有文件存储、消息附件引用、常见文件文本提取和清理。 |
| `app/services/artifact_renderers.py`、`worker_tools.py` | 生成常见格式文件，按会话授权列出或读取旧产物，并装配本轮已授权用户 Tool。 |
| `app/services/user_tools.py`、`tool_environments.py` | Tool 定义、授权、版本快照、自然名称映射，以及依赖准备任务、锁和缓存校验。 |
| `app/services/script_sandbox.py`、`mcp_tools.py` | 在 Docker 中执行脚本，通过 MCP Streamable HTTP 发现和调用远端工具。 |
| `app/services/knowledge.py`、`knowledge_jobs.py` | 知识库授权、完整索引代、检索审计，以及可恢复的后台解析和原子发布。 |
| `app/services/knowledge_parser.py`、`knowledge_backend.py`、`embedding_adapter.py` | 文档结构与偏移、原生全文/向量后端、独立嵌入协议和维度验证。 |
| `app/core/enums.py` | 跨模块业务类型、状态、角色、可见性和策略的统一枚举。 |
| `app/models`、`app/db`、`migrations/versions` | 业务表、数据库连接及 Alembic 迁移。 |

业务库保存用户、Agent、会话、Run、可见消息、事件和文件元数据。Checkpointer 按 `thread_id` 保存 LangGraph 状态，可能包含历史消息和工具结果；它与业务库不是同一个事务；可见消息只能重建受限的文字上下文，不能还原完整执行状态。文件正文在私有本地目录或 S3，不放入消息表或 SSE。进程心跳与 Run 租约心跳分别记录；反向代理和进程管理器应分别监测 API 与 worker。

### 核心业务关系

| 对象 | 关系与用途 |
| --- | --- |
| Agent、共享、会话 | Agent 归创建者所有；`yp_agent_share` 只授权指定用户使用配置。被授权用户创建并拥有自己的会话、Run 和文件，不继承 Agent 所有者的会话或文件。新会话继承 Agent 默认模型，之后独立保存当前模型；显式切换时更换 `thread_id` 并携带已完成文字历史。当前线程尚未完成过回答且其中历史 Run 均已终结、未调用工具时，下一轮也可更换线程恢复；旧 Run 与检查点保留。 |
| Run、消息 | 每次发送创建一个 Run 和一条用户消息；Run 保存提交时的 Agent 配置快照、状态和预算。完成后追加助手消息。会话消息接口按消息 ID 顺序组成前端对话列表，单轮消息接口只查看指定 Run。 |
| 事件、检查点 | Run 事件按 Run 内序号记录排队、运行、文本增量、工具和终态，供快照与 SSE 恢复；Checkpointer 保存模型内部状态。事件流不替代最终消息。 |
| 上传附件 | 上传文件先写入 `yp_artifact` 和私有存储；提交 Run 时，用户消息通过 `yp_agent_message_attachment` 保存有序引用及是否进入模型上下文。前端从用户消息的 `attachments` 展示原文件。 |
| 生成产物 | 模型调用工具后，文件以 `pending` 暂存并关联 Run；Run 完成时发布为 `ready`，写入 `artifact_published`。前端从对应助手消息的 `artifacts` 展示预览或下载入口，文件内容通过独立授权下载接口获取。`context_artifact_ids` 列出本轮提供的上传参考文件，`read_source_artifact_ids` 列出生成前工具成功读取的旧产物。 |

页面恢复以会话 `messages` 为对话展示来源，以 Run `snapshot` 和 SSE 追平进行中的状态；无需对每条消息再单独请求产物列表。文件引用的 `available` 只表示当前元数据状态，真正下载仍逐次检查所有权和期限。

前端在用户消息的 `attachments` 展示上传文件，在助手消息的 `artifacts` 展示生成文件；两者的 `artifact_id` 都可用于授权下载。`included_in_context` 表示该上传文件是否在本轮交给模型。`context_artifact_ids` 是本轮提供的上传文件 ID；`read_source_artifact_ids` 是生成前由 `read_generated_artifact` 成功读取的同会话旧产物 ID，按首次读取顺序保存到 `yp_artifact_source`，源文件删除或过期后仍保留历史关系。产物详情与列表也返回这两个数组。读取记录不证明模型实际采用了哪些文件、段落或数据，也不等同于已有单值 `source_artifact_id` 的显式来源声明；逐项来源核对仍需要更细的引用证据。
