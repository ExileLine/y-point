# M3 Runtime 契约

## 所有权与身份

- `run_id` 标识一次提交和一次终态；重试必须创建新 Run 和新提交键，不能把失败 Run 改回排队。
- `thread_id` 标识跨轮检查点上下文，同一所有者可复用；同一线程同时只能有一个非终态 Run。
- `checkpoint_id` 是 LangGraph 已持久化状态的版本，不是公开游标。检查点只由 worker 访问，不向 API 或 SSE 返回。
- 运行使用提交时固定的模型、预算和工具开关；领取及工具调用仍须重新检查当前用户、模型和资源权限。

## 状态转换

| 当前状态 | 允许的后续状态 | 条件 |
| --- | --- | --- |
| `queued` | `running`, `cancelled`, `timed_out` | 原子领取且未过排队期限；所有者取消；排队期限到期 |
| `running` | `waiting_for_approval`, `completed`, `failed`, `cancel_requested`, `timed_out` | 保存审批及检查点；回答与完成同事务；受控故障；取消信号；执行时限 |
| `waiting_for_approval` | `running`, `cancelled`, `timed_out` | 已批准且检查点与动作摘要匹配；拒绝或取消；审批到期 |
| `cancel_requested` | `cancelled`, `failed`, `timed_out` | 执行器确认停止；租约过期或受控故障；执行超时 |

`completed`、`failed`、`cancelled`、`timed_out` 是终态。状态更新以版本号和租约条件原子执行，并在同一业务事务中追加状态事件。过期租约默认进入 `failed/lease_expired`，不自动重放模型或工具。只有可证明无工具、当前 Run 已结束的检查点允许补写最终回答。

执行器续租监视器无法确认租约时会立即取消正在执行的模型或工具任务。仍持有租约的 Run 记录脱敏的 `failed/lease_monitor_error`；若取消请求已提交，则确认 `cancelled`；租约已转移或状态已变化时，旧执行器只返回最新状态，不覆盖新持有者的结果。业务库持续不可用时无法提交终态，仍由后续租约到期回收处理。

## 数据与写入顺序

1. 创建事务同时写入 Run、用户 Message 与序号为 1 的 `queued` Event；提交后 worker 才能发现任务。
   同一所有者的 `request_key` 有唯一约束；并发提交撞上约束时，API 回滚并按提交键重新核对输入与配置，完全一致才返回已有 Run，否则返回 409。无匹配提交键的其他约束失败不会被当作幂等成功。
2. 领取事务写入租约、`running` 状态与事件。工具计数和 `tool_started` 同事务；工具结束单独写 `tool_completed` 或 `tool_failed`。取消或崩溃后允许只存在开始事件。
3. LangGraph Checkpoint 保存完整运行时状态；业务库只记录对应的 `checkpoint_id`。审批中断的检查点先持久化，再由业务事务写入审批摘要、检查点 ID 与 `waiting_for_approval` 状态。
4. 完成事务同时写入最终 assistant Message、`completed` 状态与事件。检查点和业务库不能跨库原子提交；若检查点已完成而业务事务未提交，仅在归属标记匹配、`tool_count=0`、图无待执行步骤且末尾为非空最终回答时补偿。
5. 检查点缺失、提前前进或审批摘要不匹配时停止继续执行，并记录脱敏 `checkpoint_mismatch`。不能证明无副作用的情况需人工核对后创建新 Run。

## 事件协议

- 协议版本当前为 `1`。`(run_id, sequence)` 唯一，Run 内序号从 1 单调递增；事件先入库，再由查询或 SSE 发布。
- `/events?after=N` 和 SSE `Last-Event-ID: <run_id>:<sequence>` 返回序号大于游标的事件。客户端按 `(run_id, sequence)` 去重；未知事件类型按相同游标推进并忽略其展示。
- `state`、`text_delta`、`tool_started`、`tool_completed`、`tool_failed`、`artifact_published` 是当前公开类型。事件只携带可见文本、脱敏故障码、工具名、服务端调用 ID 或已发布产物 ID；不携带工具参数、结果、凭据、隐藏推理或文件正文。
- 显式选择导出回答时，worker 在 Run 完成后另行发布 TXT 产物。发布事件可能晚于 `completed` 事件；SSE 在原有空闲窗口内等待产物发布，断线或窗口结束后客户端用原游标补取事件或查询产物列表。
- Message 是用户输入和最终可见回答；Event 是可回放的过程；Checkpoint 是私有恢复状态。Event 与 Message 的文字不应被当作检查点或重放工具的依据。

## 预算、取消与副作用

- 模型和工具调用在开始前按有效租约计数。Token 与估算成本在上游响应后累计，单次请求可能超额。排队期限只约束 `queued`，审批等待另用审批到期时间。
- 运行中取消先进入 `cancel_requested`，由 worker 取消协程后确认终态；阻塞的外部工具必须自行支持取消或受单次工具时限约束。若取消与超时终态竞争，执行器遇到版本冲突后有限次重读：已提交的取消请求优先被确认，已提交的超时终态不被后到取消改写；每次状态转换只追加一个连续序号的事件。
- Tool Registry 声明启用条件、读写属性、恢复策略和是否审批；执行器统一计数并记录脱敏事件。当前 `list_text_artifacts` 只返回本人小型 TXT 元数据，`read_text_artifact` 是只读、需审批的正文工具，两者每次执行都重新核对租约、用户、模型及供应商连接，读取还复核文件授权和哈希。
- 注册入口拒绝缺少审批或幂等/补偿声明的写工具；实际接入外部写工具前还必须实现幂等键、可验证补偿或人工核对流程，不能仅依赖声明。不得把检查点恢复视为外部写操作的重试许可。
- `RuntimeRunConfig` 固定本次 Run 的归属、线程、模型和工具名；`DeepAgentsRuntimeAdapter` 只负责图的创建、状态读取和事件流。平台执行器负责数据库事务、授权、取消、超时和恢复决策。

## 部署边界

`CHECKPOINT_BACKEND=sqlite` 保留私有文件检查点；`postgres` 使用独立 DSN 和 LangGraph 官方 PostgreSQL Checkpointer，业务数据库可以独立选择 SQLite、MySQL 或 PostgreSQL。worker 启动时初始化检查点表；全局检查点读取故障会阻止本轮过期租约回收，避免未完成补偿就判定 `lease_expired`。SQLite 与 PostgreSQL 检查点不会自动互迁，切换前须处理未完成 Run 和已有线程。隔离 PostgreSQL 验证已覆盖审批断线恢复及连续文本 Run；生产仍需验证账户最小权限、TLS、备份恢复、保留清理、真实模型长任务与多 worker 故障。生产数据库与对象存储的真实故障验收单独记录，不由本地测试代替。
