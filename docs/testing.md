# 数据库迁移与测试

执行数据库结构检查、定向回归和静态检查。

[文档总览](README.md) · [项目首页](../README.md)

## 数据库与测试

SQLAlchemy 模型在 `app/models` 注册，Alembic 以其元数据管理业务表。修改模型后新增迁移；升级目标库前先备份并核对当前版本，再运行 `uv run alembic upgrade head` 和 `uv run alembic check`。应用启动时会检查业务模型需要的表和列；缺少迁移会在启动阶段报错。业务数据可选择 SQLite、MySQL 或 PostgreSQL；Checkpointer 独立配置。跨数据库场景需要对应的独立测试环境，测试结果须注明实际后端与跳过项。

常用验证命令：

```bash
uv run pytest -q
uv run alembic check
uvx ruff check app ypoint_cli migrations tests scripts sandbox local_run.py
uvx ruff format --check app ypoint_cli migrations tests scripts sandbox local_run.py
```

`ypoint_cli/` 是统一命令实现。部署测试通过 `python -m ypoint_cli` 验证真实命令入口，CLI 专项另覆盖安装后的 `yp` 和外部工作目录调用。参数、打包配置及标准库模块入口见[命令行说明](cli.md)。

代码与提交约定见[贡献指南](../CONTRIBUTING.md)。

## 按变更范围验证

先运行覆盖当前变更的测试文件，例如部署入口修改可运行：

```bash
uv run pytest -q tests/test_cli.py tests/test_deploy_all.py tests/test_deployment.py tests/test_dev_launcher.py tests/test_sandbox_image_retention.py
```

前端类型与生产构建：

```bash
npm --prefix web ci
npm --prefix web run build
```

`web/package.json` 中的 `verify:*` 是按功能划分的浏览器脚本，需要可访问的前端地址与本机 Chrome/Playwright 环境。按脚本说明设置 `WEB_BASE_URL`，只运行与变更相关的项目。多数页面验证使用拦截 API，不代表真实模型或后端全流程联调通过。

数据库方言、真实模型、Docker、OCR 或对象存储相关测试需要对应依赖或显式启用；跳过项应与通过项分别记录。优先使用临时数据库和文件目录，不直接用个人业务数据执行破坏性场景。

纯文档变更核对相对路径、锚点、代码块和实际命令入口即可，无需为排版变更运行整个业务测试集。

## 专题回归入口

按变更范围选择相关文件，使用上面的 `uv run pytest -q <测试文件>` 执行；表中的文件名均位于 `tests/`。

| 变更范围 | 主要测试文件 |
| --- | --- |
| MCP 连接、鉴权与同步 | `test_mcp_connections.py`、`test_mcp_auth_sync.py`、`test_tool_display_names.py` |
| MCP 连接共享与固定版本 | `test_mcp_connection_sharing.py`、`test_agents.py`、`test_user_tools.py` |
| MCP 审批与对话放行 | `test_mcp_policy.py`、`test_mcp_execution_policy.py`、`test_mcp_session_approval.py`、`test_mcp_approval_continue.py` |
| MCP Run 会话与资源回收 | `test_mcp_run_sessions.py`、`test_m51_governance.py` |
| MCP 上下文、结果与动态搜索 | `test_mcp_context_budget.py`、`test_mcp_results.py`、`test_mcp_tool_search.py` |
| 取消后继续与检查点恢复 | `test_cancelled_context.py`、`test_run_executor.py`、`test_tool_failure_recovery.py`、`test_session_models.py`、`test_runtime_checkpoint.py` |
| AG-UI 提交与公开历史 | `test_ag_ui.py`、`test_ag_ui_version_history.py`、`test_agent_sessions.py`、`test_session_models.py` |
| 知识格式、PDF/OCR 与删除 | `test_knowledge_formats.py`、`test_knowledge_pdf_acceptance.py`、`test_knowledge_deletion.py` |

数据库变更同时运行对应的 `tests/test_*migration.py`，在临时数据库中核对升级、旧数据读取、结构一致性与回退保护。SQLite 结果不能代替 MySQL/PostgreSQL 的对应环境验证；测试中用受控模型、回环 MCP 或内存 S3 替换的部分，也不能证明真实上游已验收。

本文维护可复用的测试入口与环境约定。特定日期的验收结论、现场脚本和执行经过按职责写入本地 TODO：服务端、真实模型、夹具镜像及数据库迁移记入 `y-point-server-todo.md`，页面与浏览器复验记入 `y-point-web-todo.md`。跨端验证分别记录实际覆盖范围，避免把受控接口结果写成真实业务验收。
