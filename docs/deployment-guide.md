# Y-Point 部署手册

本文用于从项目源码部署到本机 Docker 或单台 Linux 服务器。先完成基础部署与管理员创建，再在页面配置模型。Dockerfile、Compose、进程、日志与安全边界的设计说明见 [部署架构与配置参考](deployment.md)，逐步学习说明见 [Docker 部署逐步讲解](deployment-walkthrough.md#部署文件与执行过程)。

本文使用 `python3 -m ypoint_cli`，Docker 宿主机只需 Python 标准库。已安装项目依赖的开发环境可将此前缀替换为 `uv run yp`，激活虚拟环境后可用 `yp`；三种方式调用同一实现。全量参数和打包配置解释见 [统一命令行与项目配置](cli.md)。

## 1. 准备部署主机

主机需要安装并启动 Docker Engine（macOS 可用 Docker Desktop）、Docker Compose 插件 v2.24+、Python 3.12+。宿主机不需要安装 Node、uv、PostgreSQL 或项目 Python 依赖，它们在镜像内安装。只有 local HTTPS 自动生成自签证书时需要宿主 OpenSSL。

```bash
python3 --version
docker version
docker compose version
```

通过 SSH 登录服务器或打开本机终端，将完整项目放到目标机器，例如 `/srv/y-point`。下文所有命令均在该机器的项目根目录执行，不在容器内部执行，也不依赖 PyCharm。

```bash
cd /srv/y-point
```

本机使用实际项目路径替换 `/srv/y-point`。模块入口应在源码仓库根目录执行；已安装 `yp` 时可从其他目录运行 `yp --project-dir /srv/y-point deploy --sandbox`，公共参数放在子命令之前。`--directory` 和证书的显式相对路径按调用时的目录解析。

## 2. 一键部署

以下部署、协议切换及更新示例均包含 `--sandbox`，用于运行 Python/Bash Tool 和 Skill 受管脚本。它会让受信任的 API/worker 管理宿主 Docker，适用于自己控制的本机或单机环境；正式多租户执行的隔离要求见[沙箱说明](#可选本机脚本沙箱)。只需要聊天、知识库和内置文件生成时，可按该节明确选择无沙箱部署。

### 本机 Docker 模拟生产

```bash
python3 -m ypoint_cli deploy --sandbox
```

首次运行默认部署 local 环境，HTTP 地址为 `http://127.0.0.1:18080`。local 是部署配置的名字，应用仍使用关闭 DEBUG 的 production 配置。

脚本依次检查 Docker/Compose、初始化配置、校验 Compose、构建应用及沙箱镜像、启动 PostgreSQL、Redis、迁移、API、worker 和 Web，等待健康检查并显示状态和访问地址。不传协议参数时，新配置默认 HTTP，已有配置保留原来的 HTTP/HTTPS。

首次部署也可以同时创建管理员：

```bash
python3 -m ypoint_cli deploy --sandbox --create-admin admin
```

`--sandbox` 开启脚本执行，`--create-admin admin` 创建首位管理员，两者相互独立。后续更新使用 `python3 -m ypoint_cli deploy --sandbox`，无需再次创建管理员。

在终端输入两次密码；密码不放在参数或配置文件中。已有管理员时不要继续传 `--create-admin`，后续账号通过管理页面/API 创建。省略该参数时，脚本会打印单独创建管理员的命令。

### 内网 Linux 服务器，使用 HTTP

```bash
python3 -m ypoint_cli deploy --sandbox --environment production --http \
  --bind-ip 0.0.0.0 --http-port 18080 --create-admin admin
```

浏览器访问 `http://服务器内网IP:18080`。`0.0.0.0` 表示监听主机所有 IPv4 网卡；也可以将 `--bind-ip` 换成服务器实际内网 IP。按访问范围放通主机防火墙/安全组的 TCP 18080。浏览器和服务器分开时，浏览器里的 `localhost` 指向浏览器所在机器。

如果想用不带端口的 `http://服务器内网IP`，首次部署把 `--http-port` 改为 `80`。默认监听地址是 `127.0.0.1`，仅本机可访问；服务器部署必须按需明确监听地址。

### 可选 HTTPS

本机自签证书调试：

```bash
python3 -m ypoint_cli deploy --sandbox --https
```

访问 `https://127.0.0.1:18443`，浏览器需信任或确认 local 自签证书；证书有效期 30 天。HTTP 自动跳转 HTTPS。

服务器提供证书与私钥：

```bash
python3 -m ypoint_cli deploy --sandbox --environment production --https \
  --bind-ip 0.0.0.0 --http-port 80 --https-port 443 \
  --tls-cert /absolute/path/fullchain.pem \
  --tls-key /absolute/path/privkey.pem
```

访问证书对应的 `https://域名`，按范围放通 80/443。证书的域名匹配、DNS 和续期由部署者管理。内网 HTTP 完全不要求这些 HTTPS 配置。

### 可选本机脚本沙箱

**使用 Python/Bash Tool 或 Skill 受管脚本时必须启用；“可选”表示可以明确选择仅运行基础功能。** 上述主线命令已经包含它。若此前按不带沙箱的命令部署，可在当前任务结束后运行以下命令补开，保留配置、数据和已有管理员：

```bash
python3 -m ypoint_cli deploy --sandbox
```

额外构建 `Dockerfile.sandbox`，并将 `docker-compose.sandbox.yml` 合并到部署配置，使 API 和 worker 可以调用宿主 Docker。沙箱镜像名从实际 Compose 应用配置中的 `SCRIPT_SANDBOX_IMAGE` 读取。用户脚本容器在调用时临时创建，不是另起一个常驻 sandbox 服务。

此方式沿用项目的受信任本机调试方案。Docker socket 提供宿主引擎管理能力，正式多租户执行需要另行完成专用引擎与隔离设计。沙箱开关不会持久化为后续命令的默认值。启用后，后续一键部署及 Compose 命令继续传 `--sandbox`；遗漏该参数重新部署，会按基础配置重建容器并移除 API/worker 的 Docker 接入。

仅运行基础功能时，可以明确选择：

```bash
# 仅聊天、知识库、内置文件生成等功能；不包含自定义脚本执行。
python3 -m ypoint_cli deploy --create-admin admin
```

此模式下，Python/Bash Tool 的依赖准备、试运行和正式调用，以及 Skill 受管脚本和 MCP stdio 均不可用；需要先完成脚本试运行才能发布的版本也会受影响。远程 MCP 和不含受管脚本的 Skill 不依赖这个开关。已有管理员省略 `--create-admin admin`。

<a id="脚本参数"></a>

### 部署命令参数

```bash
python3 -m ypoint_cli deploy --help
```

| 参数 | 用途 |
| --- | --- |
| `--environment local\|staging\|production` | 默认 local；决定独立配置目录和 Compose 项目，production 额外合并资源覆盖。 |
| `--directory /absolute/path` | 自定义私有配置与数据根目录，默认 `.deployment/<environment>`。同一环境的后续命令继续使用同一路径。 |
| `--http` / `--https` | 显式选协议；不传时保留已有协议，新配置默认 HTTP。 |
| `--bind-ip IPv4` | 显式设置或更新监听地址；新配置默认 `127.0.0.1`。 |
| `--http-port N` / `--https-port N` | 显式设置或更新端口；新配置分别默认 18080/18443；不传保留已有值。 |
| `--tls-cert PATH` / `--tls-key PATH` | 必须成对提供；同时提供也会启用 HTTPS。 |
| `--sandbox` | Python/Bash Tool、Skill 受管脚本与 MCP stdio 必需；构建沙箱镜像并开放受信任的本机 Docker 接入。每次部署均需携带，不会自动记住。 |
| `--skip-build` | 复用已有镜像，跳过应用和沙箱构建；启用沙箱时会检查对应镜像已存在。适合镜像已经准备好的机器。 |
| `--wait-timeout N` | 健康等待时限，默认 180 秒；首次启动较慢可增加。镜像构建不计入这个时限。 |
| `--create-admin USERNAME` | 就绪后交互创建首位管理员，仅首次使用；自动化环境省略此参数。 |

失败时脚本返回非零并保留现场，打印日志命令；修复后可以重跑。脚本不自动安装主机软件、不删除数据、不自动备份或回滚数据库。运行中的已有部署会按传入参数更新；更新前先按第 7 节备份并安排维护窗口。

## 3. 手动部署：理解一键脚本执行的步骤

以下以受信任单机的 production 内网 HTTP 与脚本沙箱为例，所有 Compose 命令均携带 `--sandbox`；在本机学习时，省略 `--environment production`，监听使用默认值即可。只选择基础功能时省略沙箱参数和沙箱镜像构建步骤。

### 第一步：初始化独立配置

```bash
python3 -m ypoint_cli init --environment production --http \
  --bind-ip 0.0.0.0 --http-port 18080
```

生成 `.deployment/production/compose.env`、`application.env`、`database.env`、`redis.env` 以及 `data/`。默认 HTTP 不创建 `tls/`；HTTPS 才准备证书。初始化不启动容器，不导入开发机数据，不创建账号。

重复执行保留数据库与 Redis 密码、JWT 密钥、供应商凭据加密密钥、已有配置和数据。协议、监听地址和端口只在显式传参时调整；缺损配置必须恢复原配置，不自动生成一套新密钥。

### 第二步：校验 Compose

```bash
python3 -m ypoint_cli compose --environment production --sandbox -- config --quiet
```

成功通常无输出。不要直接将完整 `config` 输出贴到聊天或公共日志，其中可能包含解析后的密码。

### 第三步：构建镜像

```bash
python3 -m ypoint_cli compose --environment production --sandbox -- build
# 独立构建脚本执行镜像；Compose build 不会替你构建这个临时执行镜像。
docker build -f Dockerfile.sandbox -t y-point-script-sandbox:1 .
```

构建 API/worker 共用的后端镜像、独立前端镜像和脚本沙箱镜像。`y-point-script-sandbox:1` 对应默认的 `SCRIPT_SANDBOX_IMAGE`；若已自定义该应用配置，构建标签需与之相同。一键脚本会读取实际配置自动完成这一步。PostgreSQL 与 Redis 使用镜像仓库的现成镜像。首次构建需要访问依赖与基础镜像源；有代理或内网镜像仓库时，先配置 Docker 和构建网络。

### 第四步：启动并等待健康

```bash
python3 -m ypoint_cli compose --environment production --sandbox -- up -d --wait --wait-timeout 180
python3 -m ypoint_cli compose --environment production --sandbox -- ps
```

数据库健康后执行一次性 Alembic 迁移；迁移成功且 Redis 认证健康检查通过后启动 API 和 worker；API 健康后启动 Web。五个常驻服务应为 healthy，migrate 正常退出为 0，不需要持续运行。

### 第五步：创建首位管理员

```bash
python3 -m ypoint_cli compose --environment production --sandbox -- exec api python -m app.cli.create_admin admin
```

交互输入两次密码，仅在新数据库首次使用时执行。后续更新不重复创建管理员。

### 第六步：访问与业务配置

访问 `http://服务器内网IP:18080`，登录后配置供应商、模型和 Agent，并验证实际模型连接。容器中的 `127.0.0.1` 指向容器自己；本机模型服务地址需要改为容器可达的地址。Docker Desktop 可使用 `host.docker.internal`，Linux 根据实际网络与模型服务监听设置。

## 4. 环境、配置与镜像

`init` 通过显式 `--environment` 判断环境，未传默认 local，不根据主机名、操作系统或 Git 分支猜测。

| 环境 | 默认私有目录 | Compose 项目名 | 默认应用镜像 |
| --- | --- | --- | --- |
| local | `.deployment/local` | `y-point-local` | `y-point-server:local`、`y-point-web:local` |
| staging | `.deployment/staging` | `y-point-staging` | `y-point-server:staging`、`y-point-web:staging` |
| production | `.deployment/production` | `y-point-production` | `y-point-server:production`、`y-point-web:production` |

默认项目名也决定 PostgreSQL 与 Redis 卷名；自定义环境变量 `COMPOSE_PROJECT_NAME` 会覆盖项目名。不要给同一主机上的不同部署使用相同项目名。不同配置目录本身不会生成新的项目名；同一环境多实例需要显式管理项目名和端口。基础 Compose 还固定发布 PostgreSQL 到宿主回环地址 `127.0.0.1:15432`（容器内为 `5432`），API 和 Redis 不发布宿主端口。`--bind-ip` 与 `--http-port` 只调整 Web，不调整数据库映射；同一主机并行运行多个环境前，需要在 Compose 中移除或调整数据库宿主端口，并核对 Web 端口。

- `compose.env`：镜像名、监听地址/端口、协议、挂载路径、UID/GID、CPU/内存及日志轮转参数。
- `application.env`：应用运行配置、JWT/供应商密钥、数据库连接、模型预算、文件和脚本目录等。
- `database.env`：PostgreSQL 初始化用户、数据库和密码。数据库已初始化后，修改此文件不会自动修改卷内密码。

不要把仓库根 `.env.example` 直接替换到容器：部署应用配置由 `deploy/config/application.env.example` 生成。私有目录已被 Git 与 Docker 构建忽略，迁移机器时需要单独安全传输并保留权限。

Compose 的合并由 `ypoint_cli/deployment.py` 顺序传入多个 `-f` 实现：

```text
docker-compose.yml                     基础拓扑，默认 HTTP
  + docker-compose.production.yml      仅 production：资源覆盖
  + docker-compose.https.yml           保存的协议为 HTTPS 时：证书和端口
  + docker-compose.sandbox.yml         显式 --sandbox：本机 Docker 接入
```

后面的配置按 Compose 合并规则覆盖或扩展前面配置，同名服务仍是同一个服务；`--sandbox` 与 HTTPS 相互独立。

镜像标签不是固定 `:local`。例如发布标签可在本环境 `compose.env` 设置：

```dotenv
YPOINT_SERVER_IMAGE='y-point-server:2026.11.14'
YPOINT_WEB_IMAGE='y-point-web:2026.11.14'
```

然后再次构建会使用这些名称和标签；若使用预先发布的镜像，先拉取所需镜像，再运行 `python3 -m ypoint_cli deploy --sandbox --environment production --skip-build`。初始化保留手动设置的镜像名，不会因为重复执行而改回默认标签。

## 5. HTTP / HTTPS 切换

已有部署不会因为升级代码而自动从 HTTPS 降为 HTTP。使用一键脚本显式切换，完成配置同步、构建与重建：

```bash
python3 -m ypoint_cli deploy --sandbox --environment production --http
```

切换 HTTPS 使用 `--https`，首次提供证书；后续可复用保存的证书。local 可自动生成自签证书。示例中的 `--sandbox` 在切换协议时继续保留，避免切换后脚本执行失效。

协议写入 `compose.env` 的 `YPOINT_HTTPS_ENABLED`，`compose` 自动读取，不需要每次传协议参数。初始化同时设置应用的 `STREAM_COOKIE_SECURE`，让 HTTP 下的 SSE 票据 Cookie 可以发送；Cookie 仍保留 HttpOnly、SameSite=Strict、Run 路径及有效期约束。

如果 HTTPS 由外层网关终止、项目内部用 HTTP，可在 `application.env` 手动设置 `STREAM_COOKIE_SECURE=True` 并重建服务；后续不带 `--http`/`--https` 的初始化会保留这个值。显式切换协议会重新配套设置 Cookie。切换后若浏览器仍保留旧 Secure 票据，清理站点 Cookie 后重新登录。

## Redis 配置与旧部署升级

默认部署包含带独立随机密码的 Redis，API/worker 等待其健康后启动。Redis 不发布宿主端口，应用通过内部地址 `redis:6379` 访问。首次部署无需额外参数；已有部署更新代码后，在维护窗口执行：

```bash
# 会更新部署容器；本机环境省略 --environment production。
python3 -m ypoint_cli deploy --sandbox --environment production
python3 -m ypoint_cli compose --environment production --sandbox -- exec -T redis redis-cli ping
```

返回 `PONG` 表示认证连接成功。一键脚本会为旧三文件配置补齐 `redis.env` 和连接参数，保留数据库密码、业务密钥和协议。升级示例已包含 `--sandbox`；仅基础功能的部署可省略。已托管 Redis 配置损坏时应从备份恢复，不要删除凭据重新生成。

内存/CPU、AOF、日志和备份说明见 [Redis 配置参考](deployment.md#redis-连接持久化与容量)。应用与 Redis 密码分别位于 `application.env`、`redis.env`，两者必须一致，不能提交到 Git；健康检查自动从环境读取密码，日常无需手动复制。

## 6. 数据位置与日常运维

数据库和检查点保存在 PostgreSQL 命名卷，Redis AOF 保存在独立 `redis-data` 命名卷；上传文件、依赖及运行文件在本环境 `data/` 下。宿主机通过 Docker 查询卷路径：

```bash
docker volume inspect y-point-production_postgres-data --format '{{.Mountpoint}}'
docker volume inspect y-point-production_redis-data --format '{{.Mountpoint}}'
```

普通 Linux rootful Docker 的常见路径是 `/var/lib/docker/volumes/y-point-production_postgres-data/_data`；自定义 Docker data-root 或 rootless 模式会不同，以 inspect 为准。Docker Desktop 的路径在 Linux VM 内，不等于 macOS 同路径。通过数据库工具备份，不直接编辑运行中的 PostgreSQL 数据文件。

查看状态、日志与空间：

```bash
python3 -m ypoint_cli compose --environment production --sandbox -- ps
python3 -m ypoint_cli compose --environment production --sandbox -- logs --since 15m --timestamps api worker web redis
python3 -m ypoint_cli compose --environment production --sandbox -- logs --follow --tail 100 worker
df -h .deployment/production/data
du -sh .deployment/production/data
docker system df
```

Nginx 请求 ID 对应 API 的 `rid`，Run ID 用于追踪后台执行。业务审计在数据库。容器 stdout/stderr 由 Docker `local` 驱动默认按每容器 `20m × 5` 轮转并压缩；这限制容器日志保留量，不是整个宿主磁盘的硬配额。可在 `compose.env` 设置 `YPOINT_LOG_MAX_SIZE` / `YPOINT_LOG_MAX_FILES`，重建容器生效。文件、数据库及依赖缓存空间仍需单独管理，正式跨发布保留日志需接入采集或先导出。

停止并保留数据：

```bash
python3 -m ypoint_cli compose --environment production --sandbox -- down
```

普通停止不要加 `-v`：`down -v` 会删除 PostgreSQL 与 Redis 卷。删除私有部署目录还会丢失文件和加密密钥。

## 7. 更新、备份与排查

更新前备份数据库、检查点、Redis 数据卷、`data/`、四份私有配置及启用 HTTPS 时的证书，并安排业务维护窗口。数据库和文件需要取一致的备份点，完整要求见 [备份与恢复](deployment.md#迁移升级备份与恢复)。

保留私有目录更新代码后，重新执行原来的一键部署命令；已有管理员不再带 `--create-admin`，脚本沙箱部署始终带 `--sandbox`：

```bash
python3 -m ypoint_cli deploy --environment production --sandbox
```

本机环境省略 `--environment production`；自定义私有目录时继续传原 `--directory`。脚本会复用配置，构建镜像并执行所需迁移。回退镜像不会自动回退数据库结构，版本回退前必须核对迁移兼容性。

失败时先看终端输出和脚本给出的日志命令。常见问题：

| 现象 | 检查与处理 |
| --- | --- |
| Docker 不可达 | 确认 Engine/Desktop 已启动，当前用户可执行 `docker info`。 |
| Compose 不存在 | 安装 Docker Compose 插件，命令应为 `docker compose`。 |
| 容器启动慢、健康等待超时 | 查看 db/migrate/api/worker 日志，修复后重跑；必要时增加 `--wait-timeout`。 |
| 服务器页面无法访问 | 核对 `--bind-ip`、对外端口、防火墙/安全组和浏览器使用的服务器 IP。 |
| HTTPS 证书报错 | 核对证书和私钥配对、有效期、域名；local 自签证书需浏览器信任。 |
| 脚本 Tool 不可用 | 核对是否显式启用 `--sandbox`、沙箱镜像和 socket 权限；Linux GID 处理见架构参考。 |
| 日志显示迁移或数据库密码错误 | 恢复与数据库卷匹配的配置；不要删除配置重新 init 生成不同密码。 |
| Redis 不健康或提示 NOAUTH / WRONGPASS | 先重新执行 init 补齐旧配置，再核对 application.env 与 redis.env 是否来自同一套备份；Redis 密码不通过命令行传入。 |
| 自动化终端不能创建管理员 | 部署时省略 `--create-admin`，完成后通过 SSH 交互终端单独创建。 |

部署就绪只说明服务启动和探针通过；实际供应商连接、完整模型会话、目标容量与备份恢复需按实际环境验证。本机的可选只读核对脚本为 `ypoint_cli/verify.py`，使用前核对其 localhost/本机证书假设，不把本机探针直接视为任意域名的生产验收。
