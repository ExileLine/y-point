# Docker 部署前置准备：2026-11-13 本机验证

范围为独立 Docker Compose 模拟生产，不导入开发业务库，不执行真实模型任务。采用 macOS / ARM64、Docker Engine **29.8.0**、Compose **v5.5.1**；应用使用 production 配置，入口为本机 HTTPS。目标服务器、正式证书、其他 CPU 架构、真实模型及 M8/M11 总验收不在本记录通过范围。

## 实际构建与启动

- 根 `Dockerfile` 构建 `y-point-server:local`，API/worker 共用镜像；`web/Dockerfile` 构建 `y-point-web:local`。uv 按锁文件安装运行依赖，Alembic 已从开发组移至运行依赖，无启动时安装依赖。
- Docker 基础镜像长连接下载曾持续停滞。使用本机临时分段下载、摘要核对后载入基础镜像，再完成正常 Dockerfile 构建；没有修改 Docker 全局镜像源、代理或清理其他项目资源。联网条件恢复后可以直接复用原构建命令，网络绕行工具不作为项目依赖。
- `python3 scripts/deploy.py init --docker-gid 0` 生成私有配置、独立密钥和本机 TLS 证书；`compose --sandbox -- config --quiet` 通过。未向镜像复制本机 `.env*`、数据库、原件、日志或 macOS 虚拟环境。
- `compose --sandbox -- up -d --wait --wait-timeout 180` 成功；`db/api/worker/web` 均 healthy，迁移任务退出码 0。仅暴露 `127.0.0.1:18080` 和 `127.0.0.1:18443`，API 和数据库不发布宿主端口。
- PostgreSQL 当前业务迁移为 **055 (head)**，LangGraph 检查点表在同一实例/数据库中独立存在。应用进程按宿主 UID/GID 运行，API/worker/Web 使用只读根文件系统及可写 tmpfs。

构建、启动、升级、冒烟日志保存在本机 `.deployment/local/`：`build.log`、`startup.log`、`upgrade.log`、`verification.log`；这些是本机过程证据，不提交到 Git。

## 真实部署冒烟

命令：

```bash
python3 scripts/verify_deployment.py --sandbox --restart
```

全部通过：

| 验证 | 证据与边界 |
| --- | --- |
| TLS 与站点 | 使用生成证书的 CA 校验 HTTPS，而非关闭校验；HTTP 308 跳转、首页和 Agent/会话/知识库 SPA 路由可读；未知 API 与不存在的 asset 返回 404，不返回 SPA 假成功。 |
| API / worker | API 健康协议、worker 聚合存活及本容器 UUID 对应的心跳探针正常。 |
| 迁移一致性 | 运行镜像内 `alembic check` 无新差异；业务库 head 为 055。 |
| 日志 | 检查实际四个常驻容器均使用 `local` / `20m` / `5`；API 与 Nginx 日志可找到同一响应请求 ID；worker 不另建 `.logs/worker.log`。未以灌满磁盘方式测试 Docker 轮转。 |
| PDF/OCR 依赖 | 镜像内成功加载 pdfplumber/PDFium/Pillow；Tesseract 同时列出 chi_sim 与 eng，中文字体匹配 Noto。 |
| 真实脚本 | API 和 worker 分别执行真实 Python 沙箱，输入 41 返回 JSON 42；源码目录按同一宿主绝对路径挂载，用户容器禁网、只读、无 socket。 |
| 真实依赖代理 | API 和 worker 分别创建下载代理、内部网络，等待真实代理进程监听并退出清理；验证受信任代理脚本的宿主路径可见性，不以此证明任意公网包源可用。 |
| 重建恢复 | 写入专用探针文件后重新创建 API；Nginx 通过 DNS 重新连接，原文件仍可读；探针文件已删除。未借此宣称在途模型 Run 无损恢复。 |

额外在 worker 容器中以镜像中文字体生成扫描图并执行真实 Tesseract，正确识别“五千元”和“财务部”。该步骤证明当前镜像的字体及 OCR 可执行，不替代真实业务 PDF 的完整解析/检索质量验收。

## 首次 PostgreSQL 检查发现并修正的问题

首次 `alembic check` 发现四张 LangGraph 自管表被识别为待删除，以及两处业务列注释与 ORM 不一致，验证如实失败后完成修复：

- `migrations/env.py` 仅排除独立反射且不属于 ORM 的 `checkpoint_migrations/checkpoints/checkpoint_blobs/checkpoint_writes`，保留原有全文索引例外。没有生成删除检查点表的迁移，也没有广泛忽略所有未知表。
- 新增 `055_align_runtime_column_comments.py`，同步 Run 模型时限、Tool Skill 归属的两处中文注释；类型、约束和业务值不变。SQLite 不为注释重建表，升级/降级仅推进版本；MySQL 的目标环境仍未验证。
- SQLite 自动化验证：真实检查点同名表不触发删除差异，额外业务表仍触发检查失败；注释迁移降至 054 再升 head 通过。
- 在本次专用 PostgreSQL 容器中新建随机验证数据库，实际执行 `054 → 055 → 054 → 055`，两次 head 检查均通过，验证数据库已删除。日志 `.deployment/local/postgres-migration.log`。主开发库和原有 `postgres16` 容器未用于此验证。

## 自动化结果

组合命令：

```bash
uv run pytest -q \
  tests/test_deployment.py tests/test_worker_logging.py \
  tests/test_tool_environments.py tests/test_user_tools.py \
  tests/test_m51_governance.py tests/test_runtime_checkpoint.py \
  tests/test_runtime_limits.py tests/test_worker_monitor.py tests/test_db_schema.py
```

**68 passed, 4 skipped, 3 warnings，66.58 秒**。跳过为需要显式配置的 PostgreSQL 检查点集成、本地 Ollama、MySQL 迁移、完整 PostgreSQL M3 跨进程组合；不计通过。本轮另做的实际容器检查点启动和 PostgreSQL 注释迁移验证只证明各自范围，不替代这几个完整专项。warning 来自已有 pytest 临时目录清理冲突，不是应用故障。

覆盖新增的部署凭据/证书重复初始化保留、配置缺损拒绝、不同环境目录误用拒绝、生产证书要求、Compose 沙箱覆盖的权限边界、worker 标准流及子进程输出、逐 worker 探针、迁移比较边界，及既有工具/恢复/运行配置回归。

独立真实代理测试：

```bash
YPOINT_TEST_DEPLOYMENT_PROXY=1 uv run pytest -q tests/test_deployment_proxy.py
```

**1 passed, 3 warnings，2.01 秒**。使用已构建的最终 Web 镜像、隔离网络和最小 SSE 上游，真实 HTTPS 下间隔 1.25 秒发送两个事件，验证浏览器等价 HTTP 客户端逐条收到、Cookie 转发、外来请求 ID 被覆盖且上下游一致；随机测试容器和网络已清理。上游为受控协议替身，不是实际模型会话或完整业务审批/重连验收。

全仓 Ruff/格式检查和差异检查通过；镜像内前端 TypeScript/Vite 生产构建通过，保留原有大 chunk 提示。

## 保留的门槛

- 真实服务器的 Docker/CLI 兼容、TLS/DNS、防火墙、UID/GID、镜像 digest/漏洞扫描与交付；其他 CPU 架构、MySQL、S3、跨主机文件/引擎路径。
- 当前 socket 覆盖仅供用户控制的本机环境。正式强隔离需专用执行主机/引擎；`:ro` socket、非 root 或只读根不能提供 Docker API 只读授权。
- Docker 日志轮转是每容器保留量，不是全局磁盘配额或长期审计。重建后的旧容器日志不保证保留，正式环境需日志采集、磁盘/健康/失败率告警和数据库/文件保留策略。
- 完整用户浏览器流程、实际 Secure Cookie SSE 会话、真实模型/外部写工具、容量压测、备份/恢复及故障演练继续归 M8/M11；不据本轮勾选总体验收。

本次未创建业务账号、Agent、供应商或真实模型 Run；用户仍通过交互式 `create_admin` 初始化首位管理员。原有开发服务、开发数据库和原始文件不参与该独立部署。
