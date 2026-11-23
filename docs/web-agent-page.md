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
| 工具版本更新 | `GET/POST /api/v1/agents/{id}/tool-upgrades` | 所有者预览固定/目标版本、Schema、授权与审批状态，明确选择后升级；期望版本、预览摘要和稳定请求 ID 保护并发与重试。 |
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


## 工具版本更新

详情中的“检查工具版本更新”读取当前已发布版本，展示参数结构和授权/审批变化；阻断项禁选，审批后可调用的工具允许升级。确认仅修改选中绑定，新 Run 使用确认版本，旧 Run 保留原版本并继续受当前撤权约束。普通 Agent 编辑保留仍选中工具的固定版本，新加入的工具才采用当前版本；共享 Agent 不隐式授予工具或连接权限。

远端 MCP 函数的发现、同步和重新发布在 Tool 页的连接管理中完成，发布不会自动升级 Agent。具体流程与鉴权配置见 [MCP 同步发布](tools.md#同步发布与-agent-版本升级)。

## 按 MCP 连接选择工具

编辑器的「MCP 连接工具」展示拥有或指定共享给当前用户的连接摘要，目录来源为 `GET /api/v1/mcp-connections/catalog`。每个连接明确选择已发布工具子集，支持批量选择当前可用项；保存 `mcp_connections=[{connection_id,tools:[{tool_id,tool_version}]}]`，与直接 `tool_ids` 分开保留。相同工具同版本在 Run 去重，不同版本提示先统一；已有选择通过详情的版本升级入口升级，不因批量选择或普通编辑静默更换版本。

目录错误会保留原选择、显示重试入口；当前不可访问的关联仍可移除。新发布工具不会自动加入既有选择。共享 Agent 的接收者在提交时仍需独立取得连接摘要和相应工具子集、必要的凭据借用授权；缺权时返回连接/工具 ID 与修复提示。共享配置管理入口位于 Tool 页「MCP 连接 → 连接共享」，完整权限和迁移契约见 [按连接关联 Agent 与指定用户共享](tools.md#按连接关联-agent-与指定用户共享)。
