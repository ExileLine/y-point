# M5.1 治理与验收契约

更新日期：2026-11-03。本文约定 Tool/MCP/Skill/脚本依赖的基础治理，不将本机证据等同于目标部署总验收。

## 版本、凭据与授权

Tool 定义仍以不可变版本关联 Agent/Run，代码、Schema、依赖、连接和凭据不会被后续编辑静默替换。启停、私有化、删除、凭据撤销和共享授权是调用时持续复核的规则；快照不能绕过撤权。

| 操作 | API 规则 | 旧版本与运行中的行为 |
| --- | --- | --- |
| 保留凭据 | PUT 时省略 `mcp_bearer_token`，或传空字符串 | 保留当前版本密钥，仍受既有撤销水位约束 |
| 替换/轮换 | PUT `expected_version` 与非空 `mcp_bearer_token` | 发布新版本，将此前版本纳入凭据撤销水位，取消引用旧凭据的运行 |
| 清除 | PUT `clear_mcp_credentials=true`，不能同时替换 | 新版本不带凭据；匿名发现须成功，旧带凭据运行取消 |
| 紧急撤销 | POST `/api/v1/tools/{id}/credentials/revoke`，传 `expected_version` | 不访问上游即可推进撤销水位、禁用 Tool、取消相关运行；不会删除历史审计 |
| 关闭共享 | PUT `share_credentials=false`；公开带凭据的配置须同时转私有或清除凭据 | 其他用户的旧 Run 也受当前共享规则限制 |

排队与待审批运行直接结束；运行中任务进入 `cancel_requested`，正式 worker 监视取消并停止模型/工具。已经发出的上游操作无法撤回，外部结果不明时须核对，不能以本地取消成功证明远端无副作用。密文保留在旧版本用于历史核对，响应、事件和日志不回传明文密钥。

## MCP 接收与发现预算

- 每条连接包含初始化、发现、调用和 SSE 的累计接收预算，默认 1 MiB；在字节交给 SDK 前执行。超限或非法声明长度关闭原始响应。
- 请求使用 `Accept-Encoding: identity`，拒绝上游压缩编码，避免解压绕过预算；保留公网 HTTPS、DNS 全结果校验、固定目标 IP、TLS 原域名和拒绝全部重定向。
- 发现覆盖分页，默认最多 128 个工具、总结果 256 KiB；单项名称 128 B、描述 4 KiB、Schema 16 KiB。重复名称、循环游标和过多空页均拒绝。
- 发现总时限 15 秒；正式调用由单次工具、整轮 Run 与连接读取时限共同约束。调用结果仍执行解析后的 64 KiB 限制。

## 外部写入恢复

远端只读声明为真时可以按只读发布；没有声明时保守按写操作处理。管理员核对端点实际行为后，可以显式确认缺少声明的工具为只读；远端明确写声明不能降为只读。声明只是接入契约，远端实现和权限仍须现场核对。

外部写操作当前只开放具备幂等键的实现，补偿式恢复另行验收：

1. 版本声明 `effect=write`、`recovery_policy=idempotent` 和顶层字符串 `idempotency_key_field`，该字段必须存在于远端 Schema。
2. 管理员验证重复提交不会重复产生副作用，以及返回丢失后的查询/人工核对方式。通过 `/tools/{id}/write-contract/verify` 提交固定版本及证据引用；数据库只保存证据引用 SHA256，审计记录验收动作。
3. 模型工具 Schema 隐藏幂等键。平台根据 Run 和模型调用 ID 生成稳定键，发送前覆盖该参数；同一调用已有执行记录时拒绝再次发送。
4. 成功保存 `completed`；超时、异常、取消或进程失联导致结果不能确认时保留 `unknown`。已经终结的 Run 遗留 `running` 调用由资源核对转为 `unknown`，不自动重放。

历史未声明/未验收的写 Tool 保留定义及审计，调用会被阻止。发布新版本后写契约验收状态重置。仅填写幂等键字段不等于远端已具备幂等能力。

## 跨进程并发

`yp_tool_execution_gate` 的固定行写锁串行化名额判定，`yp_tool_execution_lease` 持久化占用。SQLite 使用写事务，PostgreSQL/MySQL 使用行锁；计数读取使用当前锁定读，避免 MySQL 可重复读快照造成超卖。表间不设置数据库外键。

| 类别 | 默认上限 | 满额行为 |
| --- | --- | --- |
| 工具调用、MCP 发现、脚本试运行 | 全局 16，用户 4，Run 2 | API 返回 429；运行返回脱敏 `tool_capacity_exceeded`，不发送工具请求 |
| 依赖准备 | 全局 2，用户 1 | 保留 `pending`，后续轮询继续领取 |

审批暂停不占执行名额。调用成功、失败和取消均释放；宿主强杀时保留至“操作时限 + 30 秒”到期后再领取。限制是单个共享业务数据库内的全局值，所有进程须使用一致配置与时钟。依赖任务自己的版本租约继续负责互斥领取和准备结果提交。

## 清理、容量及部署范围

新建容器、代理与临时网络带 `y-point.managed`、共享目录作用域摘要和到期时间；临时目录和新缓存带 `.yp-resource.json`；缓存发布前写入并纳入完整性摘要，发布后数据库引用尚未提交时强杀产生的孤立缓存也可按标记回收。正常退出沿用即时清理，常驻 worker 另每 60 秒核对，过期资源等待 300 秒宽限后回收。手动执行：

```bash
uv run python -m app.cli.reconcile_tool_resources
```

核对只触及本部署标签及标记，先删除过期容器/代理，再删除网络。仍有有效资源租约时保守保留临时目录；有任何历史 Tool 版本引用的环境目录不删除，包括逻辑删除的版本。未标记的历史 Docker 资源、目录及缓存自动回收不在本命令的删除范围内。

构建前在共享文件锁下预留 512 MiB，并检查现有缓存、在建预留和文件系统余量。默认应用预算 8 GiB、最低余量 1 GiB；占用/预留到预算 80% 或余量不足时输出告警，容量不足的新准备任务返回脱敏失败。运行中继续执行单文件及构建目录限制。

这些措施是应用预算、准入和故障回收，**不是文件系统硬磁盘配额**。需要硬隔离时须在目标主机提供专用卷/文件系统配额并实际验证；磁盘块、inode、Docker 数据目录和日志也须纳入部署预算。当前本机验证范围为单机 Docker、相同基础镜像、共享环境目录。跨主机分发、远端卷锁语义和目标平台 wheel 不能从本机结果推断。

## 迁移与兼容

`041_tool_governance.py` 增加凭据撤销水位、恢复与验收字段、资源闸门及租约。历史读取版本回填 `safe`，历史写版本回填 `manual` 且未验收，既有内容摘要不重写。另修复 `040` 在 MySQL 非空变更时丢失的 `requirements/environment_state/environment_attempts` 中文注释。

目标实例升级前备份业务库和检查点；执行 `alembic upgrade head`、`alembic check`，再重启 API/worker。开发 SQLite 已在线备份至 `.run-private/backups/y_point_pre_041_20261103_093748.db`，从 `040` 升级至 `041`，升级前后 `quick_check=ok`。回退会移除治理字段及短期租约，生产回退前必须停止领取新任务并核对进行中的外部调用，不能把回退当作撤销远端副作用。

## 验收入口与边界

```bash
# 治理与真实容器故障测试，数据保存在隔离临时目录
uv run --no-sync pytest -q tests/test_m51_governance.py tests/test_user_tools.py tests/test_tool_environments.py

# 在随机隔离数据库升级、回退重升、结构核对、跨进程争抢与 HTTP/worker 验证
YPOINT_TEST_MYSQL_M3=1 uv run --no-sync pytest -q tests/test_db_schema.py::test_mysql_m3_migrations
YPOINT_TEST_POSTGRES_M3=1 uv run --no-sync pytest -q tests/test_db_schema.py::test_postgres_m3_migrations

# 真实模型、Docker、独立 worker、公开只读 MCP；不会复用现有用户和会话数据
YPOINT_TEST_OLLAMA_M51=1 uv run --no-sync pytest -q tests/test_m51_live_acceptance.py
```

数据库连接使用运行环境配置；本机 PostgreSQL 容器映射在 5432，现场命令须与实际端口一致。测试自动删除随机隔离库，不对现有 MySQL/PostgreSQL 业务库升级。SIGKILL 测试真实强杀宿主进程，只加速清理时钟，不假设 Python `finally` 能在强杀后执行。

已通过隔离 SQLite 完整回归 184 项；默认跳过 17 项中的 MySQL/PostgreSQL 迁移与跨进程竞争已另行启用通过 2 项，真实模型组合链另行通过 1 项。治理测试另使用真实 MCP SDK + FastMCP 的进程内 HTTP 验证有效/失效/轮换 Bearer、远端 429 及错误脱敏；此处仅替换底层网络，不关闭正式实现的公网校验。真实模型链使用离线 wheel、两个临时用户、旧/新 Agent 固定版本和 DeepWiki 审批恢复。

浏览器双用户流程、具体公网 MCP 服务的鉴权/失效行为、具体外部写服务的幂等/返回丢失、目标平台 wheel、在线/离线交付、生产资源预算和责任人仍须逐项现场验收。缺少浏览器控制工具或目标环境时保持未验收，不据自动化通过勾选 M5/M5.1 总项。
