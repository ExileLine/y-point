# Y-Point 文档

[返回项目首页](../README.md)

本目录按使用任务组织文档。首次接触项目建议先完成 Docker 启动和一次模型对话，再按需阅读接口与实现细节。命令默认在仓库根目录执行，文中源文件路径也相对于仓库根目录。

## 从这里开始

| 你想做什么 | 从哪里开始 |
| --- | --- |
| 先运行完整服务 | [项目快速开始](../README.md#快速开始) |
| 部署到内网服务器，设置端口或 HTTPS | [部署手册](deployment-guide.md) |
| 修改代码并运行开发服务 | [本地开发](local-development.md) |
| 配置管理员、供应商与模型 | [认证、授权与模型配置](authentication-and-models.md) |
| 调整附件容量、超时、Token 与预算 | [配置与运行限额](configuration.md) |
| 了解代码组织与核心对象 | [架构与业务](architecture.md) |

## 使用与接口

| 文档 | 内容 |
| --- | --- |
| [Run、会话与实时恢复](runs-and-sessions.md) | 运行与检查点、会话模型、SSE、历史消息、产物、失败续聊与操作分支。 |
| [文件存储与附件](artifacts.md) | 上传与下载、存储后端、授权、保留期限和开发环境清理。 |
| [Skill 文件包与报告模板](skills.md) | 草稿与发布、文件包、脚本、固定版本、DOCX 模板及字段契约。 |
| [Tool、脚本沙箱与 MCP](tools.md) | Tool 生命周期、依赖准备、执行链、离线 wheel、授权和治理。 |
| [知识库与检索](knowledge-base.md) | 文档格式、PDF/OCR、索引发布、混合检索、引用、删除与恢复。 |

各主题保留接口路径、示例、状态与错误边界。在线 OpenAPI 需要启动开发 API 后访问 `/docs`；生产配置默认关闭接口文档。

## 开发与运维

| 文档 | 内容 |
| --- | --- |
| [贡献指南](../CONTRIBUTING.md) | 开发步骤、代码与文档约定、验证和 PR 说明。 |
| [数据库迁移与测试](testing.md) | Alembic、结构检查、定向回归与静态检查。 |
| [Docker 部署逐步讲解](deployment-walkthrough.md) | 从 Dockerfile、Shell 入口到 Compose、数据卷、资源与日志。 |
| [部署架构与配置参考](deployment.md) | 进程、环境、沙箱权限、日志、迁移、升级、备份与剩余边界。 |
| [DeepAgents 兼容说明](deepagents-compatibility.md) | 固定依赖与适配范围。 |
| [运行时契约](m3-runtime-contract.md) | Run 执行、持久化、实时流及恢复约定。 |
| [Tool 治理契约](m5-governance-contract.md) | 权限、审批、外部写、并发与资源治理。 |
| [知识库契约](m6-knowledge-contract.md) | 知识索引、检索与 Agent 接入约定。 |

## 前端实现参考

页面对接和设计记录适合参与前端开发时查阅；其中的阶段状态与截图不是新的用户操作要求。

- [Agent 页面](web-agent-page.md)、[供应商与模型管理](web-model-catalog-page.md)、[会话工作台](web-sessions-page.md)、[知识库页面](web-knowledge-page.md)。
- [设计简报](web-design-brief.md)、[布局规范](web-layout-spec.md)、[组件约定](web-component-guidelines.md)。

## 历史验证与研究

以下文档记录特定日期、环境和测试边界。当前操作以部署手册和专题文档为准；历史通过结果不自动代表其他平台或正式环境已验收。

- 部署：[方案资料核实](deployment-research.md)、[2026-11-13 本机部署验证](deployment-validation-20261113.md)、[2026-11-14 HTTP / HTTPS 验证](deployment-http-20261114.md)。
- 知识库：[PDF 第二阶段验证](m64-phase2-acceptance-20261113.md)、[多格式第三阶段记录](m64-phase3-implementation-20261113.md)、[文档删除与重建分离](m64-deletion-fix-20261113.md)。

## 维护约定

README 保留项目定位、主要能力、最短启动路径与文档入口；接口、配置和排查细节在对应专题维护。行为发生变化时，同步更新受影响的主题和入口；历史验证记录保留形成时的范围，不把历史状态直接写成当前保证。
