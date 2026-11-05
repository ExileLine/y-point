# 模型与供应商接口交接

管理员页面位于 `/models`。标签顺序为「供应商 → 模型」，默认打开供应商；手机端使用逐项列表。创建连接按 `GET /api/v1/providers/kinds` 的字段清单显示表单。编辑时仅提交变更字段，密钥与自定义请求头留空即保留原值，界面只展示“已配置”。

| 操作 | 接口 | 状态与边界 |
| --- | --- | --- |
| 连接列表与配置 | `GET/POST /api/v1/providers`、`PUT/DELETE /api/v1/providers/{id}` | 管理员权限；409 显示活跃 Agent 引用或关联模型冲突。 |
| 连接探测与发现 | `POST /api/v1/providers/{id}/test`、`GET /api/v1/providers/{id}/available-models` | 展示可用性、耗时和脱敏故障类型；发现失败或无候选时可手动录入。 |
| 模型目录与配置 | `GET/POST /api/v1/models`、`PUT/DELETE /api/v1/models/{id}`、`POST /api/v1/models/import` | 管理员权限；候选导入声明文本能力，仍须实际验证。价格为每百万 Token 的输入/输出价格。 |
| 模型探测 | `POST /api/v1/models/{id}/test`、`POST /api/v1/models/{id}/capabilities/test` | 测试可能产生上游费用；已验证能力与声明能力分开显示。 |
| 全局默认 | `POST /api/v1/models/{id}/default` | 页面仅对启用且已验证文本能力的模型提供此操作。 |
| Agent 模型选项 | `GET /api/v1/models/available` | 登录用户可读，只含 ID、名称、窗口；禁用、验证失效或窗口过小的模型不可选。 |

`npm run verify:models` 在模拟接口下检查连接创建/编辑、连接测试、模型发现/导入、文本能力验证、设默认及模型编辑的请求体。Chrome 1280×800 与 375×812 检查了无页面横向溢出及页面错误。`uv run pytest tests/test_provider_catalog.py -q` 覆盖目录权限、验证前后和连接配置变更后的筛选。真实上游凭据和真实账号的浏览器联调尚未验收。

![模型页面桌面版](web-models-desktop.png)

![模型页面手机版](web-models-mobile.png)

![供应商页面桌面版](web-providers-desktop.png)

![供应商页面手机版](web-providers-mobile.png)

## 2026-11-03 标签配色

状态采用浅色背景、鲜明边框和清晰文字：启用/可用为绿，待验证为橙黄，失败为红，处理中为蓝。模型能力按类型区分：文本蓝、流式青、工具紫、结构化输出绿、视觉粉紫、推理摘要橙；有勾表示已验证，虚线边框表示待验证，悬停可读验证说明。桌面与手机使用相同标签，不根据鲜亮配色推断模型已通过验证。
