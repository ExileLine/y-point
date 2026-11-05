# Agent 代表性页面（任务三第二步）

全局布局确认后选用 Agent 目录作为代表性页面。页面包括“我的 Agent / 共享给我”Tab、当前页筛选、卡片/表格切换、详情抽屉，以及本人 Agent 的创建、编辑、删除和指定用户授权。共享视图只展示服务端安全摘要，不读取或展示系统指令；卡片、表格和详情现可进入会话页，接口交接见 `web-sessions-page.md`。

## 接口交接

| 页面操作 | 当前接口 | 权限与数据边界 |
| --- | --- | --- |
| 登录与恢复 | `POST /api/v1/auth/login`、`GET /api/v1/auth/me`、`POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout` | 登录返回访问/轮换刷新令牌；本阶段浏览器会话存于 `sessionStorage`，401 且刷新失败时返回登录页。后续 W0 继续审查跨标签页和长期会话策略。 |
| 我的 Agent | `GET /api/v1/agents?page=&page_size=20` | 只返回当前用户自己的配置，含分页总数和系统指令；本页搜索仅过滤当前页，明确标为“筛选当前页”。 |
| 共享给我 | `GET /api/v1/agents/shared` | 返回共享摘要数组，无系统指令、模型配置或所有者文件。 |
| 详情 | `GET /api/v1/agents/{id}` 或 `GET /api/v1/agents/shared/{id}` | 打开抽屉时重新读取；共享详情仍只使用安全摘要。 |
| 创建/编辑 | `POST /api/v1/agents`、`PUT /api/v1/agents/{id}` | 创建用已验证文本能力的模型；表单覆盖温度、输出上限、超时和每轮调用/Token/成本预算。编辑仅提交变更字段并带 `expected_version`，409 显示冲突。登录用户均可创建自己的 Agent。 |
| 模型候选 | `GET /api/v1/models/available` | 登录用户读取受限选项，无供应商配置或密钥；目录失败时已有 Agent 仍可显示模型 ID。 |
| 授权对象 | `GET /api/v1/agents/share-targets?query=` | 登录用户按用户名至少 2 字符前缀搜索；仅返回其他有效用户的 ID 和用户名，最多 20 条。 |
| 共享授权 | `GET/POST /api/v1/agents/{id}/shares`、`DELETE /api/v1/agents/{id}/shares/{user_id}` | 所有者管理指定用户，撤权会请求取消接收方未结束的运行。 |
| 删除 | `DELETE /api/v1/agents/{id}` | 所有者确认后删除，后端撤销共享；失败时保留详情并展示错误。 |

成功响应按 `code/message/data` 读取；失败使用 HTTP 状态和公开 `message`，不把 401/403/409 当作空列表。页面后续的空、加载、错误状态专项验收属于第三步。

## 浏览器检查

- `npm run build` 通过；前端开发服务运行时，`npm run verify:agents` 在 Chrome 1280×800 和 375×812 通过模拟已登录接口的交互回归，覆盖本人/共享切换、共享系统指令隔离、详情、授权/撤权、删除、带 `expected_version` 且只提交变更字段的编辑、运行预算创建、管理员和普通用户的模型选择、表格视图和过期会话返回登录。
- `uv run pytest tests/test_agents.py -q` 通过，覆盖所有者与接收方授权规则、新增前缀搜索接口的认证与最小响应。
- 两种视口的文档滚动宽度分别为 1280px 与 375px，未见横向溢出。截图使用模拟业务数据，只证明布局和客户端行为；未使用真实账号完成后端联调。
- 本地 API 的 `GET /api/v1/health` 返回 200；未登录 `GET /api/v1/agents` 返回 401。真实用户创建/编辑、模型权限与轮换令牌的端到端联调仍未验收。
- Agent 页已按路由延迟加载；当前首页构建包约 654 KB，仍有 Vite 体积警告。后续 W0 优化模块拆分与首屏加载性能。

![Agent 页面桌面版](web-agent-desktop.png)

![Agent 页面手机版](web-agent-mobile.png)

![Agent 详情手机版](web-agent-detail-mobile.png)

![Agent 编辑手机版](web-agent-editor-mobile.png)

本页等待用户确认信息层级、内容密度和移动端工具栏后，再进行第三步状态专项验收。
