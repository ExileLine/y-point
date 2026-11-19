# Agent 页面

Agent 目录包括“我的 Agent / 共享给我”Tab、当前页筛选、卡片/表格切换、详情抽屉，以及本人 Agent 的创建、编辑、删除和指定用户授权。共享视图只展示服务端安全摘要，不读取或展示系统指令；卡片、表格和详情均可进入[会话页](web-sessions-page.md)。

## 接口交接

| 页面操作 | 当前接口 | 权限与数据边界 |
| --- | --- | --- |
| 登录与恢复 | `POST /api/v1/auth/login`、`GET /api/v1/auth/me`、`POST /api/v1/auth/refresh`、`POST /api/v1/auth/logout` | 登录返回访问/轮换刷新令牌；浏览器会话存于 `sessionStorage`，401 且刷新失败时返回登录页。 |
| 我的 Agent | `GET /api/v1/agents?page=&page_size=20` | 只返回当前用户自己的配置，含分页总数和系统指令；本页搜索仅过滤当前页，明确标为“筛选当前页”。 |
| 共享给我 | `GET /api/v1/agents/shared` | 返回共享摘要数组，无系统指令、模型配置或所有者文件。 |
| 详情 | `GET /api/v1/agents/{id}` 或 `GET /api/v1/agents/shared/{id}` | 打开抽屉时重新读取；共享详情仍只使用安全摘要。 |
| 创建/编辑 | `POST /api/v1/agents`、`PUT /api/v1/agents/{id}` | 创建用已验证文本能力的模型；表单覆盖温度、输出上限、超时和每轮调用/Token/成本预算。编辑仅提交变更字段并带 `expected_version`，409 显示冲突。登录用户均可创建自己的 Agent。 |
| 模型候选 | `GET /api/v1/models/available` | 登录用户读取受限选项，无供应商配置或密钥；目录失败时已有 Agent 仍可显示模型 ID。 |
| 授权对象 | `GET /api/v1/agents/share-targets?query=` | 登录用户按用户名至少 2 字符前缀搜索；仅返回其他有效用户的 ID 和用户名，最多 20 条。 |
| 共享授权 | `GET/POST /api/v1/agents/{id}/shares`、`DELETE /api/v1/agents/{id}/shares/{user_id}` | 所有者管理指定用户，撤权会请求取消接收方未结束的运行。 |
| 删除 | `DELETE /api/v1/agents/{id}` | 所有者确认后删除，后端撤销共享；失败时保留详情并展示错误。 |

成功响应按 `code/message/data` 读取；失败使用 HTTP 状态和公开 `message`，不把 401/403/409 当作空列表。

![Agent 页面桌面版](screenshots/web-agent-desktop.png)

![Agent 页面手机版](screenshots/web-agent-mobile.png)

![Agent 详情手机版](screenshots/web-agent-detail-mobile.png)

![Agent 编辑手机版](screenshots/web-agent-editor-mobile.png)

## 新建表单默认值

新建 Agent 的「最大输出 Token」默认留空，提交 `null`，由供应商决定默认输出额度；用户仍可填写具体上限。编辑已有 Agent 保留已保存的上限。留空不保证用满模型最大输出额度，供应商的必要参数、模型限制及单次超时继续生效。
