# DeepAgents 运行时兼容说明

说明平台与 DeepAgents / LangGraph 的接入边界。依赖版本以 `pyproject.toml` 和 `uv.lock` 为准；升级依赖时须重新核对下列调用契约。

[文档总览](README.md) · [运行与会话](runs-and-sessions.md)

## 平台与框架的职责

| 入口 | 当前职责 |
| --- | --- |
| `app/services/model_adapter.py` | 根据启用的供应商与模型构造 LangChain 模型，应用连接配置、供应商超时与模型能力边界。 |
| `app/services/runtime_adapter.py` | 创建 DeepAgents 图，配置平台工具、Skill、上下文预算和摘要；通过 LangGraph 读取状态、处理审批恢复与消费事件。 |
| `app/services/runtime_checkpoint.py` | 按配置打开文件 SQLite 或独立 DSN 的 PostgreSQL Checkpointer，并管理连接生命周期。 |
| `app/services/run_executor.py`、`run_manager.py` | 在独立 Worker 中执行任务，管理业务事务、租约、授权、预算、取消、审批及最终交付。 |
| `app/services/ag_ui_projection.py` | 从持久化业务事件生成 AG-UI 消息与活动；公开协议不直接读取私有检查点。 |

## 模型、工具与 Skill

| 供应商类型 | 模型列表 | 最小文本测试 | 运行时适配 |
| --- | --- | --- | --- |
| OpenAI 兼容接口 | `/models`，缺失时手动录入 | OpenAI 兼容聊天接口 | `ChatOpenAI` |
| OpenAI 官方 | `/models` | OpenAI 聊天接口 | `ChatOpenAI` |
| Anthropic | `/models`，缺失时手动录入 | Messages 接口 | `ChatAnthropic` |
| Ollama | OpenAI 兼容 `/models` | OpenAI 兼容聊天接口 | `ChatOpenAI` |
| Azure OpenAI | 数据面不列部署，手动录入部署名 | 部署路径 + API 版本 | `AzureChatOpenAI` |
| Gemini Developer API | 分页读取支持 `generateContent` 的模型 | `generateContent` | `ChatGoogleGenerativeAI` |
| AWS Bedrock | 区域内文本基础模型；推理配置文件手动录入 | Converse API | `ChatBedrockConverse` |

模型声明能力与管理页面的探测结果分别维护；实际使用还受模型、供应商、Agent 与资源权限约束。供应商配置和能力验证步骤见[认证、授权与模型配置](authentication-and-models.md)。

模型适配器关闭 SDK 自动重试；管理员探测对 HTTP `502/503/504` 最多重试一次，流式输出开始后不重试。逐项能力探测区分文本、流式、工具、结构化输出和视觉；OpenAI 类型另可探测 Responses API 的公开推理摘要，只检查 `reasoning.summary[].summary_text`，不读取或保存隐藏推理。其他供应商类型不因兼容聊天接口而自动获得该摘要能力。

平台按授权配置运行时工具；框架默认提供某个工具不代表用户自动拥有执行权限。Skill 说明加载、资源读取和脚本执行分别校验，使用固定版本和受控路径。脚本执行依赖平台 Docker 沙箱，不由默认 StateBackend 提供宿主执行能力，见 [Skill](skills.md) 与 [Tool](tools.md) 专题。

## 检查点、审批与公开事件

Checkpointer 保存 LangGraph 私有图状态，业务 Run、消息和公开事件保存在业务数据库中；二者不组成跨库原子事务。检查点归属、审批匹配、失败补偿和副作用恢复规则见[运行时契约](m3-runtime-contract.md)。

浏览器断开只停止观察，不取消 Worker；显式取消、工具超时与外部结果未知分别由平台处理。模型公开思考、工具活动及审批 attempt 通过 [AG-UI](ag-ui.md) 对外发布，不能将框架内部事件或隐藏推理直接交给客户端。

## 升级与验证边界

升级前应核对图创建、中间件、工具调用消息、流式事件形状、审批恢复及 Checkpointer API。先在隔离环境完成对应定向测试，再验证实际供应商、外部工具和目标存储；受控模型或单个供应商的结果不能代表其他环境。通用验证方法见[数据库迁移与测试](testing.md)。
