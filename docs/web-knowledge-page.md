# 知识库工作台交接

入口为左侧工作区「知识库」，列表路由 `/knowledge-bases`，详情路由 `/knowledge-bases/{id}`。复用现有 Ant Design 组件、Lucide 图标和 `--yp-*` 设计变量；沿用 Agent 页的标题、工具栏、卡片与抽屉节奏。桌面使用文档表格，手机改为逐项摘要，宽内容在容器内换行。

## 页面与业务流程

1. 新建知识库：默认私有、关键词检索；可设置说明、分块和重叠、嵌入 Token 与费用预算。混合检索须指定独立验证通过的嵌入模型。管理员可以选择目录中的嵌入模型并执行嵌入验证；普通用户填写管理员提供的模型 ID，最终可用性仍由后端检查。
2. 上传或更新文档：每次一个 TXT、Markdown、PDF、DOCX，单文件非空且不超过 10 MiB。浏览器选择文件不会立即发送；确认后使用单个 `file` 和当前 `expected_revision` 发送 multipart。相同内容响应去重时不创建虚假任务；同名内容变化须走更新版本接口。上传成功表示文件与任务已保存。
3. 跟踪索引：显示最近 50 个真实任务及持久化阶段、跳过标记、尝试次数、嵌入用量和公开失败码。存在处理中任务时每 3 秒刷新，其他时间每 15 秒重核资源；后台标签页暂停轮询，回到窗口时重新读取。任务详情不显示虚构百分比或预计完成时间。等待处理长期不变时提示检查独立 worker。
4. 验证检索：可以按已发布索引策略查询，也可以显式仅用关键词。支持 Top-K、向量候选阈值、词项覆盖重排与正文预算。页面展示排序得分、文档版本、固定索引、字符区间、标题或实际页码；不把 RRF 得分解释为向量相似度，也不把 `keyword_only=false` 解释为纯向量检索。
5. 查看引用：重新请求引用接口，返回成功后展示原文、SHA-256、版本与位置。文档内容以 React 文本节点展示，不执行文档中的 HTML 或脚本。引用失效时清除旧结果；知识库 404 时退出详情并清除该资源内容。
6. 指定用户共享：按用户名搜索并授予只读权限，名单按服务端返回的用户 ID 展示；撤销需要确认。共享用户可读文档目录、检索与引用，不请求任务或授权名单，也不展示上传、设置、管理入口。
7. 关联 Agent：编辑器分页读取可访问的知识库，最多关联 8 个；未发布资料明确标记。目录失败时保留原有关联，不用空列表覆盖。未修改关联时不提交 `knowledge_base_ids`；修改时随 Agent 的 `expected_version` 一起保存。共享 Agent 不扩大知识库权限。
8. 对话追溯：助手消息下的「本轮知识检索记录」按需读取该 Run 的审计，使用 `after_id` 加载更多；按固定引用读取原文。展示实际记录的查询嵌入模型、维度和 Token，说明召回记录不代表最终回答必然采用了该片段。打开的原文在窗口恢复和定时检查时重新核验可用性。

## 版本、任务与失败边界

- 「配置版本」「文档上传版本」「已发布索引」分别展示。当前配置可能领先于已发布索引；列表和详情中的策略标记说明是配置，真实检索由已发布索引决定。
- 编辑器保存打开时的 revision；409 后保留输入并要求刷新核对，不自动用最新版本覆盖。上传冲突后保留所选文件，刷新资源后由用户明确重试。
- 失败或取消不会用半成品覆盖已发布索引。重试携带 `expected_attempts`，最多三次；最新代、租约和状态仍由后端 CAS 判定。过期任务、零尝试或次数耗尽时可重建索引。
- 取消提交后按服务端状态显示，只有读取到终态才显示已取消。文档与知识库删除均等待成功响应才移除；错误响应保留资源。
- 服务端当前仍存在「混合配置的嵌入模型失效会阻断文档删除」边界。前端展示服务端错误并刷新资源，不宣称删除成功；修复及后续验收见 `y-point-server-todo.md`。
- 列表名称与权限筛选明确作用于当前页；分页总数保持服务端统计，避免把本页匹配数当成全部知识库数量。
- 页面切换、引用关闭或索引配置变化时使旧请求失效，防止延迟响应重新显示旧资料。权限检查以服务端为准，前端隐藏按钮不能替代鉴权。

## 接口映射

以下路径均以 `/api/v1` 为前缀。

| 操作 | 接口 / 参数 |
| --- | --- |
| 列表、创建、详情 | `GET/POST /knowledge-bases`；`GET /knowledge-bases/{id}` |
| 更新、删除 | `PUT /knowledge-bases/{id}` 完整配置加 `expected_revision`；`DELETE /knowledge-bases/{id}?expected_revision=` |
| 文档目录、上传、版本更新 | `GET/POST /knowledge-bases/{id}/documents`；`PUT /knowledge-bases/{id}/documents/{document_id}`，multipart `file`、`expected_revision` |
| 文档删除 | `DELETE /knowledge-bases/{id}/documents/{document_id}?expected_revision=` |
| 索引与任务 | `POST /knowledge-bases/{id}/rebuild`；`GET /knowledge-bases/{id}/jobs`；`POST /knowledge-bases/jobs/{job_id}/cancel`；`POST /knowledge-bases/jobs/{job_id}/retry?expected_attempts=` |
| 检索与引用 | `POST /knowledge-bases/{id}/search`；`GET /knowledge-bases/{id}/citations/{chunk_id}` |
| 共享 | `GET/POST /knowledge-bases/{id}/shares`；`DELETE /knowledge-bases/{id}/shares/{user_id}`；用户搜索复用 `GET /agents/share-targets?query=` |
| 嵌入验证（管理员） | `GET /models`；`POST /models/{id}/embedding-test` |
| Agent 关联 | `POST /agents`、`PUT /agents/{id}` 的 `knowledge_base_ids` |
| 对话检索审计 | `GET /runs/{run_id}/knowledge-retrievals?after_id=`，每批最多 50 项 |

## 验证入口与交付边界

在仓库根目录执行 `npm --prefix web run typecheck`、`npm --prefix web run build`。启动前端后执行 `npm --prefix web run verify:knowledge`，可以用 `WEB_BASE_URL` 指定地址、`CHROME_PATH` 指定浏览器可执行文件。脚本默认使用本机 Chrome；接口响应由浏览器拦截构造，不连接业务数据或真实模型。完整执行结果、开发进度与待验收事项记录在 `y-point-web-todo.md`。

日常联调使用仓库的 `make dev`，由现有启动脚本配置 API 代理与 worker。独立启动前端时可用 `API_PROXY_TARGET=http://127.0.0.1:7769 npm --prefix web run dev`。

截图均来自契约回归中的虚构账号与资料，不能替代真实模型或多用户后端验收。

![知识库列表（桌面）](screenshots/web-knowledge-desktop.png)

![知识库详情（手机）](screenshots/web-knowledge-detail-mobile.png)

![检索测试（桌面）](screenshots/web-knowledge-search-desktop.png)

![固定引用（手机）](screenshots/web-knowledge-citation-mobile.png)

## 新建默认检索策略（2026-11-13）

新建知识库默认选择「混合检索 · 关键词 + 向量」，须选择或填写有效的嵌入模型后创建。没有可用的嵌入模型时可以明确改选关键词检索；页面不自动降级。编辑已有知识库保留原检索策略及模型配置。
