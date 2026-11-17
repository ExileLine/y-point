# AG-UI 协议、SDK 与集成核实

核查日期：2026-11-17。用于 [Y-Point 改造方案](ag-ui-migration-plan-20261117.md)。这是资料研究，不是实现或业务验收记录。

## 结论

1. AG-UI 是 agent/UI 的事件与状态协议，HTTP/SSE 是其传输绑定之一。改造不要求更换 WebSocket。[1][2]
2. 已存在冻结 AG-UI 1.0；官方于 2026-09-18 发布冻结公告。实施应以冻结规范和 JSON Schema 为准，不照搬旧概念页。[1][3]
3. 1.0 的 HTTP/SSE 绑定**没有标准断线重放/续传机制**。`Last-Event-ID`、序号与 `transport.resumable` 的存在不等于 stock 客户端会续传。Y-Point 的持久游标、快照和重新订阅应作为明确的应用扩展。[2]
4. 生成工具参数完成和工具实际执行完成是两回事；AG-UI 的 `TOOL_CALL_END` 是前者，`TOOL_CALL_RESULT` 才带回工具结果。[4]
5. 1.0 支持 reasoning、activity、运行中断/恢复、取消 outcome、能力声明和使用量，但具体 SDK 对这些能力的覆盖仍必须用锁定版本验证。[1][5][6][7]

## 运行与审批

`RUN_STARTED` 开始一个协议 Run。正常成功以 `RUN_FINISHED` 结束；`outcome` 缺省或 `type=success` 是成功；`type=interrupt` 携带非空 interrupts；`type=cancelled` 表示取消。运行失败使用 `RUN_ERROR`。[6][8]

审批暂停的例子：

```text
业务 Run B，协议 Run A1
  RUN_STARTED
  工具请求事件
  RUN_FINISHED outcome={type:interrupt, interrupts:[...approval...]}

提交 resume=[{interruptId:..., status:resolved, payload:...}]
业务仍归属于 B，新的协议 Run A2
  RUN_STARTED
  工具执行结果和后续回答
  RUN_FINISHED outcome={type:success}
```

Run A1 不因恢复而再次打开；A2 与 A1 保持相同 thread。`parentRunId` 描述生成另一个 agent Run 的关系，不能擅自当成“上一次审批尝试”字段；Y-Point 的业务 Run 及尝试关联应放入明确的应用契约。[6][8]

工具审批 Interrupt 可使用 `id/reason/message/toolCallId/responseSchema/expiresAt/metadata`；回传使用 `ResumeEntry.interruptId/status/payload/metadata`。这些字段不能替代已有审批记录、所有权检查、版本检查与幂等决定。[6][8]

`RUN_ERROR` 可以作为唯一开始事件前置错误；也允许声明了终态后补充迟到错误报告。后者要求客户端将该 Run 改记为失败，但不重新打开 Run，也不许可继续输出正文、工具结果或共享状态。因此“任何情况下终态后绝无事件”不是完整准确的 1.0 规则。[6]

## 文本、思考、工具与活动

| 类型 | 1.0 表达 | 边界 |
| --- | --- | --- |
| assistant 文本 | `TEXT_MESSAGE_START/CONTENT/END` | 用稳定 messageId 归并，不按全 Run 拼接 |
| reasoning span | `REASONING_START/END` | 一个 span 可容纳多个 reasoning message |
| reasoning message | `REASONING_MESSAGE_START/CONTENT/END`，可有 chunk 快捷形式 | 与普通正文独立；支持 opaque encryptedValue，但 Y-Point 首期可不声明 encrypted 能力 |
| 工具调用请求 | `TOOL_CALL_START/ARGS/END` | END 仅指参数完整；ARGS 是参数文本碎片，协议允许最终文本不能解析；Y-Point 的公开参数投影可额外采用合法 JSON 作为自身契约 |
| 工具结果 | `TOOL_CALL_RESULT` | 有单独 messageId 和关联 toolCallId；无通用 `TOOL_CALL_FAILED` 标准事件可直接替换项目原错误事件 |
| Activity | `ACTIVITY_SNAPSHOT/ACTIVITY_DELTA` | 以 messageId 标识可展示活动；activityType 与 content 为应用约定，delta 使用 JSON Patch |
| 应用状态 | `STATE_SNAPSHOT/STATE_DELTA` | 不是整个会话恢复快照的同义词 |
| 应用扩展 | `CUSTOM` 和允许位置的 metadata | 不给标准事件任意加顶层字段，不发布 `RAW` 作为第二套私有协议 |

这些类型均可从冻结规范目录及 JSON Schema 追溯。[1][4][5][8][9]

Skill 不存在必须照搬的标准 `SKILL_STARTED` 事件。Y-Point 可使用 `activityType=y-point.skill` 表达加载说明、读取资料、脚本/模板与产物阶段；真实脚本调用仍保留标准工具事件。活动进度不能比真实执行证据更强。[1][8]

## SSE、恢复与取消的具体限制

标准 HTTP 绑定：一次 POST JSON `RunAgentInput`，响应 `Content-Type: text/event-stream`。每个 SSE data 携带一个协议事件对象；评论行可用于心跳。SSE `event`、`id`、`retry` 的协议含义未由此绑定定义，因此不能把命名 SSE event 当成唯一事件类型来源。[2]

必须分清三种动作：

- 新运行：POST 新 RunAgentInput。
- 审批恢复：带 resume 创建新协议 Run。
- 连接恢复：Y-Point 的快照/游标重新订阅扩展；不重新提交用户输入，不重新执行模型与工具。

标准 HTTP/SSE 的连接中断代表消费者看到 truncated run；规范明确 producer 可能继续运行，不能把断开自动解释为取消。[2][16] 建议 Y-Point 的标准 POST 门面与现有 GET 观察订阅都保持“断开只解绑观察者”，取消继续走明确业务接口；标准客户端不能据此假设自己能够回到同一条 POST 流。两类端点应分别声明，不能把现有 GET 订阅称作未经扩展的标准 POST 绑定。

Capabilities 中的 transport.resumable 仅对实现了明确恢复契约的连接有意义；首期对标准 SSE 客户端不宣称自动续传，应用扩展通过 custom capability 说明。[2][7]

## 输入与状态所有权

标准 RunAgentInput 的 messages 是此次运行的完整对话输入；规范要求服务端不能把输入当成“只追加最后一条”，再悄悄补齐调用方没有提供的历史。tools、context、state、resume 和 forwardedProps 各有规定。[10]

Y-Point 应校验 owned thread 与公开历史的一致性；若重用服务器 checkpoint，必须证明其可见历史与输入一致，冲突在开始流前拒绝。若要接受调用方任意编辑的历史，需要另建符合输入契约的上下文路径，不能暗中合并旧 checkpoint。

客户端提供的工具、状态和上下文是输入，不是授权。首期仅支持已授权服务端工具，前端工具执行、额外 provider 选项和未实现 capabilities 不能自动开启。

## SDK 与 LangGraph 集成判断

AG-UI 官方仓库包含 Python/TypeScript SDK 与 LangGraph 集成；官方文档也提供 LangGraph 的能力、工具流式与人工介入指南。[1][11]

这只证明存在参考实现，**不证明**当前 Y-Point 锁文件中的 DeepAgents/LangGraph 与最新 AG-UI 集成可直接兼容，也不证明任意版本的 `ag-ui-core`、`@ag-ui/client` 已完整覆盖冻结 1.0。本轮没有安装这些包或运行互操作程序，不编造已验证版本号。

P0 的必需结果是：记录 Python/TS 包的确切版本；跑通文本、reasoning、server tool、interrupt/resume/cancel、usage 与重订阅；确认旧事件投影与新事件都能通过冻结 schema 及真实客户端行为检查。不能只靠 schema 校验声称行为完全符合规范。[1][8]

现有 Y-Point worker 的任务领取、租约、审批、审计与工具执行继续负责业务；AG-UI 只读取公开事件并提供标准输入门面。不能把官方 HTTP 示例直接替换为在 API 进程内运行模型。[11]

## 界面参考

assistant-ui 的 Run Activity 聚合消息/工具/思考等活动，提供可展开的过程视图。其工具分组强调不丢失失败状态；其 AG-UI runtime options 区分 run、resume 与 connect。[12][13]

AI Elements 的 Reasoning 与 Tool 可借鉴进行中/结束折叠和工具状态表达。但其官方页面在核查日明确提示迁往 shadcn/ui，并计划在 2026 年 11 月底弃用原项目。因此建议只参考模式，继续使用项目已有 Ant Design，不引入旧包。[14][15]

## 来源

- [1] [冻结 AG-UI 1.0 总规范](https://docs.ag-ui.com/spec/1.0)。
- [2] [1.0 HTTP + SSE 绑定](https://docs.ag-ui.com/spec/1.0/basic/transports/http-sse)。
- [3] [1.0 发布公告，2026-09-18](https://github.com/ag-ui-protocol/ag-ui/discussions/2792)。
- [4] [1.0 Tool Calls](https://docs.ag-ui.com/spec/1.0/events/tool-calls)。
- [5] [1.0 Reasoning](https://docs.ag-ui.com/spec/1.0/events/reasoning)。
- [6] [1.0 Lifecycle](https://docs.ag-ui.com/spec/1.0/events/lifecycle)。
- [7] [1.0 Capabilities](https://docs.ag-ui.com/spec/1.0/basic/capabilities)。
- [8] [冻结 1.0 的 JSON Schema，官方仓库](https://raw.githubusercontent.com/ag-ui-protocol/ag-ui/main/docs/spec/1.0/schema.json)。其中 prose 描述与动态行为要求仍需一并核对。
- [9] [1.0 Metadata](https://docs.ag-ui.com/spec/1.0/basic/metadata)。
- [10] [1.0 Run Input](https://docs.ag-ui.com/spec/1.0/basic/run-input)。
- [11] [AG-UI 官方仓库与 LangGraph 集成入口](https://github.com/ag-ui-protocol/ag-ui)。
- [12] [assistant-ui Run Activity](https://www.assistant-ui.com/elements/run-activity)。
- [13] [assistant-ui AG-UI runtime options](https://www.assistant-ui.com/docs/runtimes/ag-ui/runtime-options)。
- [14] [AI Elements Reasoning](https://elements.ai-sdk.dev/components/reasoning)。
- [15] [AI Elements Tool](https://elements.ai-sdk.dev/components/tool)。
- [16] [1.0 传输契约与 Truncation](https://docs.ag-ui.com/spec/1.0/basic/transports)。
