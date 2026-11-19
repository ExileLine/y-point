# AG-UI 改造方案归档入口

本文原保存 AG-UI 改造前的方案、问题清单和分阶段施工安排。原文已归档到工作区 `y-point-server-todo.md` 的“README 关联文档复核与历史归档（2026-11-19）”，保留当时的阶段状态、建议模块和参考资料。

当前接口与模块职责以以下文档和实现为准：

- [AG-UI 接口与处理过程](ag-ui.md)：唯一对话提交入口、审批恢复、公开历史和持久化观察扩展。
- [运行与会话](runs-and-sessions.md)：Worker 执行、会话模型、检查点、文件和取消。
- [DeepAgents 兼容说明](deepagents-compatibility.md)：平台与框架的职责边界。
- [协议与 SDK 参考](ag-ui-protocol-research-20261117.md)：规范来源及集成约束。

实际入口为 `app/api/v1/ag_ui.py`，事件投影为 `app/services/ag_ui_projection.py`，前端协议处理为 `web/src/ag-ui.ts`，过程组件为 `web/src/components/RunActivity.tsx`。API 提交并观察持久化事件，独立 Worker 执行 DeepAgents；标准 POST 与恢复观察共用同一协议投影。数据库升级和验证入口见[数据库迁移与测试](testing.md)。
