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

运行 `uv run pytest -q tests/test_deepagents_spike.py` 可重复执行本地验证。程序使用 `FakeMessagesListChatModel`，不需要供应商凭据，也不会向外部模型发送请求。

## 已验证的 API

| 能力 | 实测结果 |
| --- | --- |
| Agent 初始化与模型回合 | `create_deep_agent(model=...)` 可接受 `BaseChatModel`；假模型完成工具前后的两个回合。 |
| 工具调用 | `tools=[echo]` 注册工具；模型产生 `tool_calls` 后，状态中保存 `ToolMessage`。 |
| 流式事件 | `stream_mode=["updates", "messages"]` 返回 `(mode, payload)`；观察到 `model`、`tools` 节点更新及 AI/工具消息。 |
| Skill 装载 | `skills=["/skills/"]` 配合 `StateBackend` 的 `files` 输入读取 `SKILL.md`；`skills_metadata` 可从检查点状态读取，不在 `invoke()` 的对外返回值中。 |
| 检查点 | `InMemorySaver` 按 `configurable.thread_id` 保存消息和 `checkpoint_id`。 |
| 审批与恢复 | `interrupt_on={"echo": True}` 在工具执行前中断；用 `Command(resume={"decisions": [{"type": "approve"}]})` 恢复后才执行工具。 |
| 取消 | 取消消费 `astream()` 的 asyncio 任务会把取消传入正在等待的异步工具；工具的清理块得到执行。 |

## 接入限制

- `FakeMessagesListChatModel.bind_tools()` 在验证程序中只返回自身；这验证编排流程，不验证供应商的真实工具协议、Token 用量或错误处理。至少一种真实模型的调用与流式输出仍需在 M2 验收。
- `InMemorySaver` 只验证当前进程中的检查点和审批恢复。进程重启恢复、持久化映射及崩溃处理属于 M3，不能从本轮结果推断已支持。
- 默认 Agent 包含文件工具、`execute` 和子 Agent 工具；本轮默认 `StateBackend` 不提供可执行沙箱。正式接入前必须由平台限制工具发现、资源访问和执行权限。
- 取消测试仅证明 asyncio 取消传递。持久化 Run 状态、跨进程取消、超时竞态及副作用幂等仍属于 M3。
- Skill 测试只验证说明文件被加载；导入校验、版本固定和用户授权属于 M5。
