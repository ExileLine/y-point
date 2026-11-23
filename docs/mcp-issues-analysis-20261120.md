# MCP Issues #1 / #3 / #4 分析与建议

分析日期：2026-11-20（Asia/Shanghai）。本地代码基线：`978a5e1`，工作区现有未跟踪业务资料未作修改。本次为静态代码与公开 Issue 分析，未执行真实模型、远端写工具、Docker 业务验证、数据库迁移或部署；本文不产生新的功能验收结论。

## 结论

三项属于同一条 MCP 接入与执行链路，但不是三份重复缺陷。#1 是连接配置与管理提案；#3 是两个使用阻断反馈；#4 是综合整改方案。建议以 #4 为跟踪主项，关联 #1 / #3，按验收项拆分，不重建已存在的连接实体，也不整包接受其中全部设计。

- #1：https://github.com/ExileLine/y-point/issues/1
- #3：https://github.com/ExileLine/y-point/issues/3
- #4：https://github.com/ExileLine/y-point/issues/4

## 当前实现核对

| 诉求 | 本地状态 | 证据与含义 |
| --- | --- | --- |
| MCP 服务实体、统一配置和保存后的发现 | 已有基础实现 | `app/models/mcp_connection.py`；`app/api/v1/mcp_connections.py:39,68,96,171`。实体为 McpConnection，不需要再增加一套 mcp_server。 |
| HTTP/HTTPS、自定义端口、内网与回环 | 已支持 | `app/services/user_tools.py:64`；`app/services/mcp_tools.py:50,102`。#1 的公网 HTTPS/443 限制是历史状态。 |
| stdio | 已有受控实现 | `app/services/mcp_stdio.py:57`，管理员固定镜像/命令，容器禁网，不是任意宿主命令执行。 |
| 无鉴权 / Bearer | 已支持 | `app/schemas/mcp_connection.py:13`；`app/services/mcp_tools.py:116`。 |
| API Key、自定义 Headers | 尚未实现 | 当前连接 Schema 没有对应字段，HTTP 构造仅处理 Bearer 和平台控制请求头。可复用 Provider 的加密容器，但需独立制定 MCP Header 校验与冲突规则。 |
| 连接级共享、跨用户分组目录 | 尚未完整实现 | 连接列表仅返回所有者资源；`share_credentials` 授权的是公开工具使用凭据，不等于共享连接实体。`/tools/catalog` 用 `_output(item)`，没有版本字段，仍不能据此完整分组共享连接。 |
| 统一更新配置后自动适用于旧工具 | 并非现有语义 | `bind_connection()` 将连接配置/凭据复制到 Tool 版本；一般更新不改变旧快照。轮换凭据会撤销旧版本、取消相关活动 Run，旧工具需要发布新版本。连接统一管理不等于旧版本自动迁移。 |
| #3 的 16 工具上限 | 当前本地已解除 | `web/src/Agents.tsx:272` 的 Tool 多选无 maxCount；`app/schemas/agent.py:71` 没有 tool_ids 长度限制。只说明本地代码，不代表提报者部署已升级；发现数量、上下文和调用预算仍存在。 |
| #3 写工具必须通过幂等契约 | 仍存在 | `app/services/worker_tools.py:483` 硬校验 idempotent、幂等字段和管理员验收。审批通过后仍会进入这道校验。 |
| 缺少 readOnlyHint 时默认行为 | 已确认缺口 | `app/api/v1/tools.py:132` 将未声明工具判为 write；`app/services/user_tools.py:131` 默认无需审批且恢复 manual；Worker 随后禁止执行。因而可以保存与关联，却无法实际调用。 |
| Run 内 MCP 会话复用 | 尚未实现 | `app/services/mcp_tools.py:203` 每次重新初始化 ClientSession 并发现工具；stdio 随单次上下文创建、清理容器。 |
| 结果大小治理 | 已有硬限制，缺少完整交付设计 | 发现与传输均有字节预算；调用 JSON 超 64 KiB 报错。不能把“已有上限”写成“已有可配置截断及原文留存”。 |

## 优先处理：把执行许可与恢复能力拆开

当前不是没有 effect / requires_approval / recovery_policy 字段，而是执行层把所有外部写的准入条件硬绑定到已验收幂等契约。建议建立三项独立判断：

1. **动作性质**：只读、写入，及读写判断来源；未声明注解按可能写入处理。
2. **执行许可**：是否需要本次用户审批，受所有者策略、使用者更严格设置和部署规则共同约束。
3. **恢复能力**：调用中断后是否可确认结果、查询状态或安全重试；人工批准不能证明幂等，也不能证明远端未执行。

建议第一版默认 `writes`：只读不额外审批，写入/未知需要审批；支持 `always`。`never` 不得绕过部署限制，首版不开放普通用户配置非幂等写免审批。已验收幂等工具同样不能覆盖明确的 `always` 设置，幂等验收不是业务授权。

对不支持幂等字段的标准 MCP 写工具，批准后可以执行一次，不强行往其 Schema 添加参数。保留现有调用 ID 防重、参数摘要绑定、当前授权与撤权检查；发送后遇超时/断连/取消/worker 崩溃，结果未知时记录 `unknown`，不自动重放。不重放只能约束平台行为，不能保证远端恰好执行一次；结果未知后也不能通过自动换调用 ID 补做来绕过。可以确认未发送的本地校验失败应与已发送后未知区分。

普通所有者可确认自己私有工具的未知注解为只读，但应记录确认来源；不允许把远端明确的写声明降为只读。工具共享后，发布者的低风险声明不能自动等价于接收者信任。只读切换应自动使用 safe 恢复策略，避免用户手工同步多个字段。

在创建、详情、目录上统一返回可执行状态与受控原因，至少区分：可直接调用、需审批、配置/授权阻断。需审批并不等于不可调用。前端应能在关联 Agent 前解释问题，而非等待首轮失败。

## #4 需要修正的几个设计点

### 审批不是只有开关

`RunApproval.actions` 目前保存工具名和 HMAC 参数摘要，`RunApproval.tsx` 主要展示摘要。普通外部写开放后，应提供授权用户可读的脱敏动作说明，继续用摘要绑定实际执行参数与版本；不能让用户只对一个不可理解的哈希批准外部写。

当前 `decide_approval()` 拒绝审批会取消整轮，恢复分支只提交 approve。#4 的“拒绝后让模型收到反馈继续回答”是新的运行/交互契约，涉及混合批次决策、检查点及 AG-UI 终态；建议首版明确保留现行拒绝即取消，若改为继续回答，单列实现与验收，不能当作已有能力。

### 按连接关联可以实现，但授权仍落在具体 Tool 上

保留 `UserTool` / `UserToolVersion` / `RunTool`，增加 Agent 的连接绑定与工具筛选，提交时从已发布、已授权工具展开并固定 RunTool 快照。连接只是选择与管理单位，不应成为绕过单工具策略的执行入口。

远端发现的新工具须确认发布，不能因连接绑定自动进入未来 Run。连接绑定和直接 tool_ids 混用时，须定义去重和版本冲突规则；同一个工具版本冲突应明确报错，不静默选最新版。共享 Agent 不自动授权私有连接、工具或凭据。

目录可提供安全的连接 ID、显示名、可用性和工具汇总；共享接收者无需获得 URL、凭据或 stdio 启动配置。保留稳定 model_name，避免破坏历史记录和 Skill required_tools。

### 完整 Run 会话复用不能作为独立小修

当前 `pause_for_approval()` 保存检查点后释放执行器租约；执行函数返回，批准后由数据库调度器重新领取，可能换 worker。原 ClientSession、stdio 管道与容器属于进程资源，不能通过 Run ID 字典自动跨进程恢复。

应先决定目标：

- 连续执行段内复用：较小改动，但遇审批、重启等边界不承诺状态连续；不能称为完整 Run 内保状态。
- 完整 Run 内复用：需要明确会话归属及租约、审批保留与过期、跨 worker 路由、强杀回收和恢复失败语义。会话所有权不能简单沿用已经被审批释放的执行租约。

复用后仍应每次调用复核权限与固定版本，并处理同一状态会话的并行调用排序、每请求超时与累计传输预算、容器保留期和资源名额。HTTP 会话失效可重新初始化，但不得重放结果不明的 tools/call，也不能声称浏览器业务状态已恢复。

当前 stdio 默认禁网。若真实验收要求外部网页操作，还需单独确认镜像能力与受控网络配置；会话复用本身不意味着浏览器 MCP 已可访问外网。

### 工具数量、模型上下文与结果限额分别治理

16 个关联上限已移除。MCP_DISCOVERY_MAX_TOOLS 控制发现规模，不应混用为 Agent 数量限制。Schema UTF-8 字节预算是部署和请求体上限，不能代替供应商真实 Token 估算；完整上下文仍要计入系统指令、历史、附件、工具描述和输出预留，复用现有窗口保护。

按需工具搜索可后续引入。第一版先完成静态 Schema 大小与完整请求预算提示，不在目录/权限链路尚未稳定时同时引入动态注册。

输出治理应区分传输安全硬上限、结构化结果有效性与模型可见内容预算。不可直接切片 JSON；如果保留原始大结果，需明确私有存储、授权、保留期和按需读取，而不是假定当前已有统一原文留存机制。

## 建议实施顺序与 Issue 管理

| 顺序 | 范围 | 完成标准 |
| --- | --- | --- |
| 第一批 | 执行许可/恢复解耦、审批策略与迁移、可调用性反馈 | 无注解工具可经审批执行一次；不支持幂等参数也可用；异常后 unknown 且不重放；旧审批和撤权不退化。 |
| 第二批，可并行拆分 | 按连接分组/绑定与确认同步；API Key/Headers | 保留旧 tool_ids，快照与权限不变；17 工具连接可绑定；新工具不自动准入；凭据不回显。 |
| 单列设计后实施 | 会话生命周期与复用 | 明确连续执行段或完整 Run 的保证；覆盖审批、跨 worker、404、取消、并发和清理。 |
| 后续优化 | 按需工具加载、结果分层交付与预算体验 | 动态发现不扩大授权，模型上下文不越预算，结构化结果与原文件仍可追溯。 |

以 #4 作为主跟踪项；#1 按已完成/剩余需求更新，#3 将 16 工具上限和写执行阻断分别跟踪。不能因本地已去除数量上限关闭整个 #3，也不能因增加连接实体关闭整个 #1。本次未对 GitHub 发表回复或更改状态。

## 建议验收场景

- 未声明注解、无幂等键的工具：默认需审批，批准后恰好一次远端调用，不添加陌生参数。
- 明确只读、所有者确认只读、明确写入三类分别验证；普通使用者不能放宽共享发布策略；模式 always 不被幂等分支覆盖。
- 审批拒绝、超时及多次审批的终态符合选定契约；参数变化或工具版本变化不能借用旧审批。
- 写调用返回丢失、超时、取消、worker 强杀后不自动重放；可以确认未发送的错误不误称已经执行。
- 连接绑定与 tool_ids 同时存在时去重；版本冲突明确失败；发现新工具、变更 Schema、撤权、轮换、停用保持固定快照与当前授权边界。
- 自定义 Headers 在创建、保留、替换、清除、发现及正式执行中一致，敏感值不进入输出、错误或日志。
- 有状态 HTTP / stdio 的多步操作、审批暂停、新 worker 恢复、会话过期、并发排序与清理；真实浏览器业务状态须另验，不能只用协议 session ID 证明。


## 官方协议与 SDK 证据附录

本附录核实官方协议及 SDK；业务实现的核对结论见上文。官方事实、项目事实和设计推论分别标注。

### 1. 版本与核实方法

- **本地只读核实**：`pyproject.toml:53` 为 `mcp>=1.30.0,<2`；`uv.lock:1783–1785` 固定 `1.30.0`；`.venv` 安装元数据也是 `1.30.0`。安装文件 `shared/session.py`、`client/session.py`、`client/streamable_http.py`、`shared/version.py` 与直接下载的官方 `v1.30.0` 文件逐一 SHA-256 比对一致。
- **官方 SDK 版本**：`git ls-remote` 确认 `v1.30.0` 指向提交 `8c2fa6eabacde869d75568e33b9d466b58c2f512`。下文 SDK 链接固定此提交。该版 `LATEST_PROTOCOL_VERSION="2025-11-25"`；支持列表另含 `2024-11-05`、`2025-03-26`、`2025-06-18`。`initialize()` 提议默认版本，但接受支持列表内的服务端返回值，**实际协议版本须看初始化结果**。[S1][S2]
- **适用范围**：以下会话、404、取消规则采用项目 SDK 支持的 **2025-11-25** 版本，不将其他版本的传输机制直接套用到当前项目。

### 2. 官方事实：annotations 是提示，不是授权或重试保证

| 字段 | 2025-11-25 规范语义 | 缺省语义 |
| --- | --- | --- |
| `readOnlyHint` | `true` 表示工具不修改其环境 | `false` |
| `idempotentHint` | 同参数重复调用不产生额外环境影响；仅在 `readOnlyHint == false` 时有意义 | `false` |
| `destructiveHint` | 可能破坏性更新；仅在非只读时有意义 | `true` |
| `openWorldHint` | 可能与开放的外部实体交互 | `true` |

规范明确：所有 annotations 均为 hints，不保证忠实描述工具行为；客户端不应根据**不可信服务器**提供的 annotations 作工具使用决策。SDK 1.30.0 的 Pydantic 字段实际默认是 `None`，上述布尔值是规范上的缺省解释，不能声称 SDK 已把缺失值自动填为这些布尔值。[S5][S6]

**结论／策略推论**：不能单凭 `readOnlyHint=true` 绕过用户授权，也不能单凭 `idempotentHint=true` 自动重放结果未知的写操作。可信来源的提示可作为策略输入；授权范围、服务端执行契约、幂等键或结果核对仍需独立依据。这里并非声称协议禁止所有重试，而是提示本身不构成授权或恰好一次执行保证。[S5]

### 3. 官方事实：会话、404 与浏览器状态

- `ClientSession` 是 SDK 客户端对象；服务端可选地在初始化响应里返回 `Mcp-Session-Id`，收到后客户端必须在后续 HTTP 请求携带。HTTP 服务可有多个连接；**Python 对象、HTTP 连接、服务端 MCP 会话不是同一个概念**。[S2][S7]
- 服务端可随时终止会话，之后对该 ID 返回 404；客户端收到携带会话 ID 的请求对应的 404 后，必须用**不带旧 ID**的新 `InitializeRequest` 建立新会话。[S7]
- SDK 1.30.0 的 POST 处理遇到 404，将该请求转换为 `Session terminated` 错误；此路径**不会自动重新初始化或重放工具调用**。SSE 的事件恢复路径与重新 POST 执行工具不同，不是业务调用的恰好一次保证。[S8]
- HTTP 断开不应被解释成取消；2025-11-25 规范要求取消应显式发送通知，事件重放能力又是可选的。[S7]

**推论：MCP 会话不等于浏览器业务状态。** 协议未定义浏览器 context、cookie、页面或业务事务如何持久化，也未规定它们必须绑定于 MCP 会话。新建 `ClientSession` 或 HTTP MCP 会话，**不必然**丢失浏览器状态；也**不保证**保留状态。必须依据具体服务如何绑定会话、进程、容器、profile 与持久存储判断。[S7]

**推论：404 不消除先前调用的不确定性。** 某次请求因旧会话无效被拒绝，与此前请求是否已经产生副作用是不同问题。对于已发送但结果因超时或断线而未知的调用，重新初始化只解决通信，不证明此前未执行、已回滚或可安全重试；应另做结果核对或依赖明确的幂等机制。[S7][S8]

### 4. 官方事实：取消、超时与默认 DELETE（重点）

#### 4.1 规范要求

`notifications/cancelled` 表示希望终止此前仍在进行的请求。接收端 **SHOULD** 停止处理、释放资源，但请求可能已完成、不可取消或处于无法停止的阶段；取消与正常完成存在竞态。这是尽力取消语义，**没有撤销既有副作用的事务／回滚保证**。客户端不得取消 `initialize`；实验性的 task 增强请求另用 `tasks/cancel`。[S9]

生命周期规范还规定：等待响应超时后，发送方 **SHOULD** 发出取消通知并停止等待；这是规范建议，须与具体 SDK 的实现区分。[S10]

#### 4.2 已核实的 SDK 1.30.0 行为

`ClientSession.call_tool()` 委托 `BaseSession.send_request()`，所查实现如下。[S2][S11]

| 场景 | 1.30.0 具体行为 | 是否自动发送 `notifications/cancelled` |
| --- | --- | --- |
| 等待请求的调用任务被取消 | `send_request()` 没有捕获取消并构造通知的分支；进入 `finally` 清除响应／进度映射并关闭响应流 | **该方法不发送** |
| 请求读超时 | `anyio.fail_after` 的 `TimeoutError` 转为超时 `McpError`，再执行上述清理 | **该路径不发送** |
| 收到对端取消通知 | 接收循环按 request ID 查找 `_in_flight`，调用对应 responder 的 `cancel()` | 这是**接收处理**，不是自动向对端发送取消 |
| 退出 `ClientSession` 上下文 | 取消内部 task group | 不能据此等同于向对端发送请求取消 |

因此，对 **1.30.0 的这些路径**，不能假定 SDK 已替调用者发送协议取消；但也**不能仅凭项目没有显式 `send_cancelled` 就断言整个系统未取消**，还需看包装层、其他通知发送方式、服务端与 transport 清理。这里没有检查项目这些路径，也不将此结论推广到其他 SDK 版本。[S11]

**默认 HTTP 退出行为**：`streamable_http_client(..., terminate_on_close=True)` 默认开启会话终止；退出 transport 的 `finally` 中，若已有 session ID，会调用 `terminate_session()` 发送 HTTP DELETE。旧名称 `streamablehttp_client` 默认值相同，并把参数传给新接口。它是 **transport 上下文退出**的行为，不是单独退出 `ClientSession` 就必然触发。DELETE 不等于请求级取消或业务回滚；服务端可返回 405，网络失败也可能发生，而且该清理 await 没有取消屏蔽，故只能说默认会尝试，不能保证已送达或已终止。[S8][S7]

### 5. 项目事实与生命周期推论（供主线程整合）

**主线程提供，未独立检查业务实现**：DeepAgents `0.7.29`；每次 invoke 新建 transport 与 `ClientSession`，执行 initialize/list_tools/call；stdio 每次新容器，默认 `--network=none`；审批暂停保存 checkpoint、释放 Run 租约并从 `execute_text_run` 返回，批准后 DB 调度可能换 worker；当前拒绝审批取消整轮。

**官方生命周期依据**：SDK 的 session／HTTP transport 依赖活跃的异步任务、内存流及上下文；HTTP transport 退出默认尝试 DELETE。`stdio_client()` 启动子进程；退出时关闭 stdin、等待退出，必要时终止进程树，并关闭管道。[S8][S11][S12]

据此得到以下**设计推论，并非 MCP 强制架构或项目现状结论**：

1. **释放 Run 执行器不等于可以退出会话所有者。** 若审批期间要复用活会话，应有跨暂停仍存活的资源所有者维护 transport、session、读写任务、子进程／容器；若相关上下文已退出，checkpoint 不能让这些活对象自动复活。`terminate_on_close=False` 也只跳过 DELETE，不会保存原客户端任务、管道或保证服务端不超时。
2. **跨 worker 不能直接恢复内存会话／stdio。** 需选定留在原所有者、向持久资源管理服务转发、或重新连接／启动并恢复业务状态的机制，并考虑 worker 死亡、会话过期、连接健康、所有权交接与重复恢复。新容器通常不共享旧进程内存；浏览器状态是否保留仍取决于实际存储和服务实现。网络限制属于容器执行约束，不是 MCP 授权或会话延续保证。
3. **明确审批与在途调用的边界。** 已发送调用在暂停／超时期间可能仍完成；恢复时不能把“没有收到结果”当成“尚未执行”。拒绝审批或取消整轮不能证明更早工具副作用回滚；终止 HTTP 会话、stdio 进程也不能撤销已提交的外部操作。
4. **记录可核对的恢复信息。** 资源归属、业务状态引用、协议／会话失效信息、已发送调用的标识与结果状态有助于安全恢复；MCP 未承诺 checkpoint 持久化、审批授权、浏览器恢复或这些业务状态机。不能仅从 DeepAgents 版本推断它已经提供上述保证。

### 官方来源

- [S1 SDK 协议常量与支持列表](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/shared/version.py)；[types.py 协议常量](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/types.py#L27)
- [S2 SDK ClientSession：initialize / call_tool](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/client/session.py)
- [S3 官网协议版本说明](https://modelcontextprotocol.io/specification/versioning)
- [S4 2026-07-28 Streamable HTTP：无协议层会话管理](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [S5 2025-11-25 Schema：ToolAnnotations](https://modelcontextprotocol.io/specification/2025-11-25/schema#toolannotations)；[Tools 提示信任边界](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [S6 SDK ToolAnnotations：Optional 字段与默认语义](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/types.py#L1247)
- [S7 2025-11-25 Transports：会话、404、断开、恢复、DELETE](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [S8 SDK Streamable HTTP：POST 404、恢复、terminate_session、两个 client 上下文](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/client/streamable_http.py)
- [S9 2025-11-25 Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation)
- [S10 2025-11-25 Lifecycle：Timeouts](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#timeouts)
- [S11 SDK BaseSession：send_request、退出、接收取消](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/shared/session.py#L240)
- [S12 SDK stdio：进程、管道与退出清理](https://github.com/modelcontextprotocol/python-sdk/blob/8c2fa6eabacde869d75568e33b9d466b58c2f512/src/mcp/client/stdio/__init__.py#L182)
