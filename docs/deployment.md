# Y-Point Docker 部署架构与配置参考

首次部署和一键脚本操作见 **[部署手册](deployment-guide.md)**；本文说明文件职责、配置和设计边界。逐步学习提要见 [README](../README.md#部署文件与执行过程)。

本方案把前端 Nginx、API、worker、PostgreSQL 拆成独立服务，用同一套 Compose 在单机交付。前后端独立构建与升级，首次部署可以放在同一台机器；不把 Vite、API、worker 塞进一个容器。默认 PostgreSQL 16 同时保存业务表和独立的 LangGraph 检查点表，原始文件及工具目录单独持久化。当前向量后端为 exact，不要求安装 pgvector。Redis、Celery 和 supervisor 均不作为这套运行链的依赖。

这是部署前置准备与本机验证基线，不代表 M8/M11、目标服务器、真实模型、容量或容灾总验收通过。Docker 官方资料和限制见 [deployment-research.md](deployment-research.md)。

## 文件与进程

| 文件 / 服务 | 职责 |
| --- | --- |
| 根 `Dockerfile` | Python 3.12，uv 锁定依赖，多阶段构建；带 Alembic、Docker CLI、PDFium 所需库、Tesseract 中英文语言包和中文字体。API 与 worker 共用该镜像。 |
| `web/Dockerfile` | Node 构建静态资源，再交给无 root 权限的 Nginx；运行时不包含 Node/npm/Vite。 |
| `docker-compose.yml` | db → migrate → API/worker，API 就绪后启动 web。只有 web 暴露端口，数据库和 API 留在 Compose 网络。 |
| `docker-compose.sandbox.yml` | 显式启用本机 Docker socket，赋予 API 试运行和 worker 正式运行所需的管理权限。 |
| `docker-compose.https.yml` | 可选 HTTPS 监听、证书挂载及 HTTP 跳转；由持久化协议开关自动选择。 |
| `docker-compose.production.yml` | 正式环境资源起点，与同一拓扑和独立 production 私有配置组合。资源值必须按目标机器实测调整。 |
| `scripts/deploy_all.py` | 一键检查、初始化、构建、启动并等待健康；可交互创建首位管理员，内部复用 deploy.py。 |
| `scripts/deploy.py` | 生成私有配置、证书、独立密钥；选择环境并透传 Compose 命令，避免漏传配置文件。 |
| `deploy/entrypoint.sh` / `preflight.py` | 密钥、目录权限和启用时的沙箱前置检查，然后 `exec` 主进程。迁移不藏在每个 API 启动流程里。 |
| `deploy/healthcheck.py` | API 检查业务健康协议；worker 核对本容器 UUID 对应的数据库心跳，不能由其他 worker 的心跳冒充健康。 |

每个常驻服务使用 Docker `init: true` 回收子进程，`restart: unless-stopped` 管理退出重启；API 直接运行一个 Uvicorn 进程，worker 使用原生 CLI。默认一个 API、一个 worker，不引入 supervisor/Gunicorn。将来按并发压测增加 API 容器，比再增加一层进程管理器更清楚。正常关闭提供 90 秒窗口；故障后仍沿用租约、检查点和副作用不自动重放的业务规则，不能承诺所有在途 Run 无损继续。

## 本机启动

需要 Docker Engine/Desktop、Compose v2.24+、Python 3.12；仅 local 自动生成 HTTPS 自签证书时需要 OpenSSL。本机已验证 Engine 29.8.0 / Compose 5.5.1；镜像内 Docker CLI 使用 29 系列，其他引擎版本的 API 协商须单独核对。请给 Docker Desktop 预留构建磁盘及运行内存；默认上限约为 API 2 GiB、worker 2 GiB、PostgreSQL 1 GiB、Nginx 256 MiB，各容器按需使用而非预占全部内存。OCR/模型请求/依赖构建并发仍受应用层限制。

在项目根目录：

```bash
# 创建 .deployment/local，默认 HTTP；配置和数据均被 Git 与镜像构建排除。
python3 scripts/deploy.py init

# 基础 Web / API / worker / 数据库部署，不授予宿主 Docker 管理权限。
python3 scripts/deploy.py compose -- config --quiet
python3 scripts/deploy.py compose -- build
python3 scripts/deploy.py compose -- up -d --wait --wait-timeout 180
python3 scripts/deploy.py compose -- ps
```

新部署默认访问 `http://localhost:18080`，直接提供页面、API 和 SSE，不生成证书、不要求 OpenSSL，也不挂载证书目录。应用环境仍为 production。HTTP 部署显式配置 `STREAM_COOKIE_SECURE=False`，SSE 仍使用短期 HttpOnly、SameSite=Strict、Run 路径 Cookie。

可选 HTTPS：

```bash
python3 scripts/deploy.py init --https
python3 scripts/deploy.py compose -- up -d --wait
```

local 的 HTTPS 使用 30 天自签证书，访问 `https://localhost:18443`，HTTP 自动跳转；浏览器需要确认例外或在本机信任证书。HTTP 与 HTTPS 共用 `deploy/nginx/locations.conf`，页面路由、缓存、上传限制、SSE 无缓冲、请求 ID 和日志规则保持一致。镜像启动脚本按 `YPOINT_HTTPS_ENABLED` 选择 HTTP/HTTPS 模板。

首次创建管理员使用交互式密码，不将密码写入命令行或 Git：

```bash
python3 scripts/deploy.py compose -- exec api python -m app.cli.create_admin admin
```

本套部署默认全新数据库，不导入开发机的 `y_point.db`、现有用户、Agent、文件或供应商凭据。宿主模型服务地址也要改为容器能访问的地址；Docker Desktop 可使用 `host.docker.internal`，容器中的 `127.0.0.1` 指向容器自身。正式模型连接请登录后配置并重新验证。

停止并保留数据：

```bash
python3 scripts/deploy.py compose -- down
```

普通 `down` 保留 PostgreSQL 命名卷及 bind 数据。`down -v` 会删除命名卷中的数据库和检查点；删除 `.deployment/local` 会丢失文件和加密密钥，都不能当成普通停止操作。

## 启用本机真实脚本沙箱

现有 API 的脚本试运行与 worker 的正式执行都调用 Docker CLI，因此本机完整功能验证需要两个受信任服务接入同一个引擎：

```bash
docker build -f Dockerfile.sandbox -t y-point-script-sandbox:1 .
python3 scripts/deploy.py compose --sandbox -- config --quiet
python3 scripts/deploy.py compose --sandbox -- up -d --wait --wait-timeout 180
```

启用后持续使用同一个 `--sandbox` 参数，包括查看、更新、停止部署。沙箱源文件、依赖环境和受信任下载代理脚本使用 `.deployment/local/data` 下的绝对路径，API、worker 和宿主 Docker 以同一路径访问；初始化脚本将代理源码原子复制到 `data/runtime/package_proxy.py`。不要随意把 bind 目录改成容器内不同路径，也不要把这一方案直接用于远程 daemon。移动部署数据目录或更换主机需要停机迁移，并核对数据库中持久化的依赖路径。

Linux socket 的 GID 与运行用户不同时，初始化可传 `--docker-gid <gid>`；Docker Desktop 通常使用容器内 GID 0。若权限失败，检查 socket 在容器中的实际 GID，再修改私有 `compose.env` 的 `YPOINT_DOCKER_GID` 并重建 API/worker。应用无需改成 root。

**权限边界：**socket 访问能管理整个宿主 Docker，挂成只读、降低容器用户权限或限制自身 Linux capabilities 都不能把 Docker API 变成只读接口。该覆盖文件适用于用户控制的本机模拟环境；正式多租户/公网执行应先采用专用执行主机或专用 Docker 引擎及相应鉴权隔离。当前项目没有独立的远程沙箱管理服务，不能用增加一个普通 socket proxy 就宣称完成了强隔离。

用户脚本容器沿用禁网、只读根、无 capabilities、非 root、资源上限与超时，不挂载 socket。脚本、依赖准备及受信任下载代理容器显式使用 `--log-driver=none`；有界输出仍通过 attach 管道交给既有业务审计，避免临时容器另写无界 daemon 日志。没有启用覆盖配置时，聊天、知识库等不依赖 Docker 的能力仍可使用；脚本试运行/依赖准备/正式执行不可用，资源核对可能报告 Docker 不可达。

## 环境与配置

每个环境独立生成 `.deployment/<environment>/compose.env`、`application.env`、`database.env` 和 `data/`，仅 HTTPS 创建 `tls/`，Compose 项目名和数据库卷也随之隔离。每份环境使用独立的 JWT、供应商 Fernet 加密密钥和数据库密码。所有私有文件默认 0600、目录 0700，并通过运行 UID/GID 访问。

local 的应用配置仍为 `production`，关闭 DEBUG、SQL echo 和接口文档；staging 使用 `staging`。协议与环境独立，新部署都默认 HTTP。内网生产首次部署不需要域名或证书：

```bash
python3 scripts/deploy.py init --environment production --http --bind-ip 0.0.0.0 --http-port 80
# 也可将 --bind-ip 换成服务器实际内网 IP。
python3 scripts/deploy.py compose --environment production -- config --quiet
python3 scripts/deploy.py compose --environment production -- build
python3 scripts/deploy.py compose --environment production -- up -d --wait
```

生产 HTTPS 显式开启，并提供证书和私钥；兼容旧命令，成对提供证书也会启用 HTTPS：

```bash
python3 scripts/deploy.py init --environment production --https --bind-ip 0.0.0.0 \
  --tls-cert /absolute/path/fullchain.pem --tls-key /absolute/path/privkey.pem \
  --http-port 80 --https-port 443
python3 scripts/deploy.py compose --environment production -- config --quiet
python3 scripts/deploy.py compose --environment production -- build
python3 scripts/deploy.py compose --environment production -- up -d --wait
```

默认监听为 `127.0.0.1`。需要其他内网机器或公网访问时，在该环境的 `compose.env` 设置 `YPOINT_BIND_IP` 为服务器网卡 IP 或 `0.0.0.0`，并按实际访问范围放行 HTTP/HTTPS 端口。HTTPS 证书更新后重建 web；重新传证书执行 init 可以复制新证书。当前 Nginx 覆盖来路的 X-Forwarded-For 与请求 ID，API 不发布宿主端口；外层 HTTPS 网关需要另行统一真实客户端 IP 和协议的可信代理链，不能直接透传任意外来代理头。

`compose.env` 供 Compose 插值，两个应用/数据库 env 文件用于运行配置。容器只读挂载应用 env；镜像不包含 `.env.production`、本机密钥或数据。`docker compose config` 的完整输出会展开秘密，日常使用 `config --quiet`。协议写入 `YPOINT_HTTPS_ENABLED='true'/'false'`，compose 命令自动选择 HTTPS 覆盖文件，无需每次重复传协议参数。建议通过 init 切换，使 Nginx 和 SSE Cookie 同步；HTTPS 终止在外部网关时，可按浏览器实际使用协议单独设置 `application.env` 的 `STREAM_COOKIE_SECURE`，重复无协议参数的 init 保留该显式值。

已有部署切换协议（以下示例为 local，生产环境在每条命令加 `--environment production`）：

```bash
# 更新代码后先构建镜像；启用脚本沙箱的部署在所有 compose 命令继续带 --sandbox。
python3 scripts/deploy.py compose -- build
python3 scripts/deploy.py init --http
python3 scripts/deploy.py compose -- up -d --wait
```

切回 HTTPS 使用 `init --https`；local 可生成自签证书，其他环境复用已有证书或提供新证书。切换保留数据库密码、JWT/供应商加密密钥、数据路径、镜像名及已有证书，HTTP 即使证书缺失也可初始化。切换后浏览器如仍保留旧 Secure 票据，可清理该站点 Cookie 后重新登录。未记录协议开关的旧版部署默认继续 HTTPS，重复 init 不会静默切换协议；缺损配置必须恢复完整原配置，不自动生成新密钥。

前端在普通内网 HTTP 来源使用 `crypto.getRandomValues()` 兼容 UUID，并对不可用的异步剪贴板提供用户点击复制回退。`STREAM_COOKIE_SECURE` 未配置时，原生应用继续按 development/test 为 False、production/staging 为 True 的默认策略运行。

init 可通过 `--bind-ip` / `--http-port` / `--https-port` 设置或更新监听参数，省略时保留已有值；仅支持 IPv4 监听参数。

CPU/内存、监听端口、镜像名、日志阈值通过 `compose.env` 的 `YPOINT_*` 参数配置；业务预算与模型限额在 `application.env` 配置。正式发布可通过 Dockerfile 的镜像构建参数与 Compose 镜像变量固定 digest/发布标签；仓库当前固定主要版本起点，尚未建立发布制品签名或镜像漏洞扫描流水线，不将 tag 视为不可变制品。

## 前后端一起与分开部署

首次建议同机独立容器：Nginx 托管静态资源并把 `/api/` 代理到 API，浏览器同源，避免额外 CORS 和跨站 Cookie 配置。Nginx 提供 SPA 回退、静态资源缓存、110 MiB 请求体上限和可选 HTTPS；单文件大小和附件数量仍由服务端强制检查。

代理不缓冲 SSE，读取超时为 3600 秒，并逐跳保留请求 ID。容器 IP 改变时通过 Docker DNS 重新解析 API。快照、事件去重、最终消息与票据恢复仍由应用处理，代理超时不是业务 Run 的总超时。

前后端镜像可独立构建：

```bash
python3 scripts/deploy.py compose -- build api
python3 scripts/deploy.py compose -- build web
# 只启动后端及其必要依赖；不启动 Web。
python3 scripts/deploy.py compose -- up -d api worker
```

跨主机部署需额外提供 API 的受限网络入口、前端网关 upstream 配置和可信代理策略；当前仓库默认内部 upstream 为 `api:8000`，不把同一主机的 Compose 网络当成跨主机服务发现。文件/依赖目录和 Docker 引擎的同路径要求也需重新设计。可在明确目标部署形态后增加该覆盖配置，首次不引入 Kubernetes、跨主机共享盘或分布式调度。

## 日志、追踪与磁盘

| 数据 | 保存与查询 | 容量与保留 |
| --- | --- | --- |
| Nginx 访问日志 | JSON stdout，包含时间、请求 ID、路径、状态、耗时；不记录 query、Cookie、Authorization 或请求体。 | Docker `local` 驱动轮转。 |
| API 日志 | 既有脱敏请求日志，包含同一 `rid`；关闭 Uvicorn 重复访问日志。Nginx 在 API 响应返回 `X-Request-ID`。 | Docker `local` 驱动轮转，不另写应用日志副本。 |
| worker 日志与子进程标准流 | `WORKER_LOG_STDOUT=True`，含现有 Run ID/故障码；本地 `make dev` 默认仍写原容量受控日志文件。 | Docker `local` 驱动轮转；不产生容器内 `.logs/worker.log`。 |
| PostgreSQL 日志 | stderr，不启用数据库内部 logging collector，不开启 SQL 全语句日志。 | Docker `local` 驱动轮转。 |
| 业务审计 | 认证/资源操作、Run 事件、模型调用、ToolInvocation 与知识检索记录保存在 PostgreSQL；请求 ID用于 HTTP 排查，Run ID用于执行链追踪。 | 随数据库备份和业务保留策略管理；不能用容器日志代替业务审计，也未宣称实现统一 request_id 到每张审计表的关联。 |

每容器默认 `max-size=20m`、`max-file=5`、压缩开启，按日志量约保留 100 MiB 的消息，而非保证保留固定天数或提供整个磁盘硬配额。修改阈值需重建容器；应用自行写其他文件不受该驱动约束。删除或重建容器后，旧容器日志不保证保留；正式跨发布追踪需先接入日志采集/远端聚合，临时诊断可在维护前导出所需范围日志到受限备份位置。Nginx 错误日志保留代理诊断，可能包含请求行；不要把凭据放进 URL。

```bash
python3 scripts/deploy.py compose -- logs --since 15m --timestamps api worker web
python3 scripts/deploy.py compose -- logs --follow --tail 100 worker
python3 scripts/deploy.py compose -- ps
python3 scripts/deploy.py compose -- stats --no-stream
# 主机和 Docker 数据空间需同时检查；Desktop 的数据盘是 VM 内磁盘。
df -h .deployment/local/data
du -sh .deployment/local/data
docker system df
```

排查顺序：从浏览器响应 `X-Request-ID` 找 Nginx/API 同一次请求，再由响应里的 `run_id` 查 worker、Run 详情/调用记录和业务审计。不要为了排查直接打印模型提示词、工具参数、原文件、Bearer 或供应商 Key。Docker 日志归 Docker 管理，不手工截断其内部文件。

文件、数据库、检查点、工具依赖缓存、镜像与构建缓存仍会增长。项目已有文件配额及工具存储预算/最小剩余空间检查，但没有整个宿主磁盘的硬配额或自动扩容。本机使用 `df`、`du`、`docker system df` 检查；目标环境需接入磁盘 80%/90%、最小剩余空间、服务 unhealthy、worker 失联和失败率告警，并明确数据库/事件保留周期。Compose 的 unhealthy 不会自动触发退出重启策略；不要用无差别自动重启掩盖数据库、磁盘或工具副作用问题。不执行全局 `docker system prune`，也不删除仍被业务版本引用的依赖目录。

## 迁移、升级、备份与恢复

当前业务迁移 head 为 `055`；业务结构由 Alembic 管理，四张 LangGraph 检查点自管表不参与业务自动迁移差异。`055` 只对齐两处历史列注释，不修改业务值；详情与复验见本次验证记录。Alembic 已列入运行依赖；镜像按锁文件安装 `--no-dev`，不依赖容器启动时下载软件。首次启动等待数据库健康后，单独执行一次 migrate；迁移失败会阻止 API/worker 启动。

每次升级先备份，并在维护窗口停止写入，再执行：

```bash
python3 scripts/deploy.py init
python3 scripts/deploy.py compose -- build
python3 scripts/deploy.py compose -- stop api worker
python3 scripts/deploy.py compose -- run --rm migrate
python3 scripts/deploy.py compose -- up -d --force-recreate --wait api worker web
python3 scripts/deploy.py compose -- exec api alembic check
```

使用脚本沙箱的部署在上述每次 Compose 调用都带 `--sandbox`。数据库迁移与检查点库不是一个跨库事务；这里放在同一 PostgreSQL 实例只简化备份，不改变业务补偿规则。

备份必须同时覆盖 PostgreSQL（包含业务与 LangGraph 检查点表）、`data/` 下私有正文/依赖、三个私有 env 文件、供应商 Fernet 密钥及启用 HTTPS 时的证书。停止 API/worker 写入后，`pg_dump -Fc` 导出并同步备份文件目录；备份保存到本机以外的受限位置并加密。示例导出目标需先创建，避免把备份写进镜像或 stdout 日志：

```bash
mkdir -p .deployment/local/backups
python3 scripts/deploy.py compose -- exec -T db pg_dump -U y_point -d y_point -Fc > .deployment/local/backups/database.dump
```

正式恢复应先在隔离环境验证：使用匹配的镜像与私有密钥、恢复数据库及同一批文件、检查迁移版本/引用/下载/检查点/沙箱依赖，再切流。绑定路径变更需专门验证。更换 `DB_BACKEND` 不会把原 SQLite 数据迁移过去，复制一份业务库也不能代替文件和检查点恢复；代码回滚是否可行取决于迁移兼容性，不能直接对有业务数据的库自动 downgrade。

## 验证范围与剩余门槛

自动化验证入口：`uv run pytest -q tests/test_deployment.py tests/test_worker_logging.py tests/test_tool_environments.py tests/test_user_tools.py tests/test_m51_governance.py tests/test_runtime_checkpoint.py tests/test_runtime_limits.py tests/test_worker_monitor.py tests/test_db_schema.py`。已启动部署可运行 `python3 scripts/verify_deployment.py --sandbox`；无活跃任务的隔离环境可加 `--restart` 验证重建。独立真实 HTTP/HTTPS 代理验证：`YPOINT_TEST_DEPLOYMENT_PROXY=1 uv run pytest -q tests/test_deployment_proxy.py`。

本机实际构建/运行结果记录在 `docs/deployment-validation-20261113.md`，与本文件的配置说明分开维护。正式服务器的 TLS/DNS、防火墙、不同 CPU 架构、镜像供应链、实际 OCR/模型/外部写服务、恢复演练、高并发及监控告警仍需按 M8/M11 发布范围验收。

参考资料：uv 的 Docker 集成、Uvicorn 的部署、Nginx 代理及 Docker 官方文档；原始地址在 `deployment-research.md` 的来源与下方列出，仓库配置值属于本项目选择。

- https://docs.astral.sh/uv/guides/integration/docker/
- https://www.uvicorn.org/deployment/
- https://nginx.org/en/docs/http/ngx_http_proxy_module.html
