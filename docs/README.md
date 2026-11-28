# Y-Point 文档

[返回项目首页](../README.md)

本目录按使用任务组织文档。首次接触项目建议先完成 Docker 启动和一次模型对话，再按需阅读接口与实现细节。命令默认在仓库根目录执行，文中源文件路径也相对于仓库根目录。

## 从这里开始

| 你想做什么 | 从哪里开始 |
| --- | --- |
| 运行服务及脚本工具沙箱 | [项目快速开始](../README.md#快速开始)，首次部署与后续更新均携带 `--sandbox`。 |
| 部署到内网服务器，设置端口或 HTTPS | [部署手册](deployment-guide.md) |
| 修改代码并运行开发服务 | [本地开发](local-development.md)，完成环境准备后运行 `uv run yp dev`，已激活项目环境时可用 `yp dev`。 |
| 选择命令入口与查阅参数 | [统一 CLI](cli.md#安装与入口)，对照 `uv run yp`、激活环境后的 `yp` 与 `python3 -m ypoint_cli`。 |
| 配置管理员、供应商与模型 | [认证、授权与模型配置](authentication-and-models.md) |
| 调整附件容量、超时、Token 与预算 | [配置与运行限额](configuration.md) |
| 了解代码组织与核心对象 | [架构与业务](architecture.md) |

## 使用与接口

| 文档 | 内容 |
| --- | --- |
| [Run、会话与实时恢复](runs-and-sessions.md) | 运行与检查点、会话模型、SSE、历史消息、产物、失败续聊与操作分支。 |
| [AG-UI 1.0 与处理过程](ag-ui.md) | 标准提交/审批恢复、持久化观察扩展、消息身份、工具与 Skill 过程和公开边界。 |
| [文件存储与附件](artifacts.md) | 上传与下载、存储后端、授权、保留期限和开发环境清理。 |
| [Skill 文件包与报告模板](skills.md) | 草稿与发布、文件包、脚本、固定版本、DOCX 模板及字段契约。 |
| [Tool、脚本沙箱与 MCP](tools.md) | Tool 生命周期、依赖准备、执行链、离线 wheel、授权和治理。 |
| [知识库与检索](knowledge-base.md) | 文档格式、PDF/OCR、索引发布、混合检索、引用、删除与恢复。 |

各主题保留接口路径、示例、状态与错误边界。在线 OpenAPI 需要启动开发 API 后访问 `/docs`；生产配置默认关闭接口文档。

## 开发与运维

| 文档 | 内容 |
| --- | --- |
| [贡献指南](../CONTRIBUTING.md) | 开发步骤、代码与文档约定、验证和 PR 说明。 |
| [统一命令行与项目配置](cli.md) | `yp` 全部子命令的 `-h` / `--help` 入口、部署参数、轻量模块入口和 `pyproject.toml` 配置解释。 |
| [数据库迁移与测试](testing.md) | Alembic、结构检查、定向回归与静态检查。 |
| [Docker 部署逐步讲解](deployment-walkthrough.md) | 从 Dockerfile、Shell 入口到 Compose、数据卷、资源与日志。 |
| [部署架构与配置参考](deployment.md) | 进程、环境、沙箱权限、日志、迁移、升级、备份与剩余边界。 |
| [DeepAgents 兼容说明](deepagents-compatibility.md) | 固定依赖与适配范围。 |
| [运行时契约](m3-runtime-contract.md) | Run 执行、持久化、实时流及恢复约定。 |
| [Tool 治理契约](m5-governance-contract.md) | 权限、审批、外部写、并发与资源治理。 |
| [知识库契约](m6-knowledge-contract.md) | 知识索引、检索与 Agent 接入约定。 |

## 前端实现参考

页面契约和设计规范适合参与前端开发时查阅；截图用于说明布局，页面验证过程另存本地 TODO。

- [Agent 页面](web-agent-page.md)、[供应商与模型管理](web-model-catalog-page.md)、[会话工作台](web-sessions-page.md)、[知识库页面](web-knowledge-page.md)。
- [设计简报](web-design-brief.md)、[布局规范](web-layout-spec.md)、[组件约定](web-component-guidelines.md)。

## 设计与调研

以下资料保留当前设计约束与参考来源，当前操作和接口以各专题为准。

- [Docker 部署调研](deployment-research.md)。
- [AG-UI 协议与 SDK 参考](ag-ui-protocol-research-20261117.md)；当前实现见 [AG-UI 接口与处理过程](ag-ui.md)。

<a id="历史验证与研究"></a>
<a id="历史报告入口"></a>

## 旧文档路径导航

以下路径为旧链接提供当前使用文档的入口，不再维护阶段计划、实现对照或本机验收报告：

- 部署：[部署验证](deployment-validation-20261113.md)、[HTTP/HTTPS](deployment-http-20261114.md)、[Redis](deployment-redis-20261115.md)。
- 知识库：[PDF/OCR](m64-phase2-acceptance-20261113.md)、[多格式处理](m64-phase3-implementation-20261113.md)、[文档删除](m64-deletion-fix-20261113.md)。
- 协议与工具：[AG-UI 接入](ag-ui-migration-plan-20261117.md)、[MCP 验证](mcp-acceptance.md)、[MCP Issues](mcp-issues-analysis-20261120.md)。

## 维护约定

README 保留项目定位、主要能力、最短启动路径与文档入口；接口、配置、使用边界和排查步骤在对应专题维护。接口移除时同步修正调用示例和模块职责；仍注册但标记弃用的接口须说明兼容范围与推荐替代入口，不能写成已移除。

过程性记录按职责保存：服务端真实模型验收、接口与 Worker 复验、迁移历史、枚举等重构经过记入工作区的 `y-point-server-todo.md`；前端设计迭代、页面交互及浏览器复验脚本和结果记入 `y-point-web-todo.md`。跨端任务分别记录对应结果，通过日期和主题关联。当前迁移操作、枚举职责、可复用测试入口和已知限制继续维护在公开专题中。

两份本地 TODO 按仓库规则不纳入 Git；公开使用说明保持独立完整，不依赖私有记录，也不将历史验收结果视为当前环境保证。
