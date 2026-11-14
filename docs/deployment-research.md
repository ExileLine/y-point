# y-point 本机模拟生产部署：Docker 官方资料核实

核实日期：**2026-11-13**。范围限定为 Docker Compose 服务划分、健康检查与依赖、环境覆盖、`local` 日志轮转、Docker socket 与宿主路径安全边界。下列“采用”是部署设计建议，不表示已验证仓库现有实现；本次未运行、停止或重建容器。

## 1. 服务按职责与权限拆分

**采用：**应用、独立后台任务（如有）、有状态依赖分别声明为 Compose service；需要 Docker API 的功能集中到专用管理服务，普通业务服务不挂 socket。官方原则是“一容器一种职责”，不是强制“一容器只能有一个进程”。

**限制：**拆分便于分别配置健康检查、日志和权限，但管理服务若可访问同一个 Docker daemon，仍能操作该 daemon 管理的其他资源；服务拆分本身不构成强安全隔离。后者是依据 daemon 默认授权模型得出的设计判断。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/build/building/best-practices/#decouple-applications
- https://docs.docker.com/engine/extend/plugins_authorization/

## 2. 依赖顺序等待“就绪”，运行期恢复另行处理

**采用：**真正影响启动的依赖使用长格式 `depends_on`，设置 `condition: service_healthy`；依赖服务定义能反映可服务状态的 `healthcheck`。一次性初始化或迁移任务（如有）用 `service_completed_successfully`。探测建议初值：`interval: 10s`、`timeout: 3s`、`start_period: 30s`、`retries: 5`，再按实际启动耗时调整；这些数值是建议，不是官方默认值。探测命令必须在镜像中可执行。

**限制：**短格式 `depends_on`／`service_started` 只保证先启动，不保证能处理请求；Compose 按依赖顺序创建服务、按反向顺序移除服务。启动等待不等于运行期持续编排：依赖之后掉线，应用仍需超时、重试和重新连接。

**采用：**常驻服务可设服务级 `restart: unless-stopped`；一次性任务不采用此策略。健康检查只改变健康状态，单纯变成 `unhealthy` 不会触发该退出重启策略。依赖项内的 `restart: true` 是另一项机制：跟随显式 Compose 更新／重启操作，不跟随容器运行时因崩溃而执行的自动重启；该字段要求 Compose **2.17.0+**。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/compose/how-tos/startup-order/
- https://docs.docker.com/reference/compose-file/services/#depends_on
- https://docs.docker.com/reference/dockerfile/#healthcheck
- https://docs.docker.com/engine/containers/start-containers-automatically/

## 3. 显式选择环境覆盖文件，检查合并结果

**采用：**基础配置保留公共服务定义，本机模拟生产配置放在单独覆盖文件；部署入口显式传入文件顺序，例如下列仅渲染配置的命令（本次未执行；文件名为建议）：

```sh
docker compose --env-file .env.local-prod \
  -f compose.yaml -f compose.local-prod.yaml config
```

**限制：**后文件覆盖前文件不等于整份替换。映射按键合并；`ports` 按 `{ip, target, published, protocol}` 唯一键合并，新端口可能与旧端口同时保留；`volumes` 按容器内 `target` 合并。不要以为写一个新端口或 `ports: []` 就清除了旧端口。需要完整替换时使用 `!override`，并要求 Compose **2.24.4+**；否则把环境专属端口只放在各自覆盖文件，避免基础文件预置。多个 `-f` 文件中的相对路径统一相对第一个基础文件解析。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/
- https://docs.docker.com/reference/compose-file/merge/

## 4. 区分配置插值与容器环境变量注入

**采用：**显式 `--env-file .env.local-prod` 提供 Compose 插值，必填值用 `${VAR:?说明}` 让缺失或空值直接报错；容器需要的变量再通过服务的 `environment`／`env_file` 明确注入。

**限制：**`.env`／CLI `--env-file` 不是自动给所有容器注入变量的机制。当前官方插值优先级为：**宿主 shell > 显式 `--env-file` > 未指定该参数时的项目目录 `.env`**；多个 `--env-file` 后者覆盖前者。默认项目目录依次由 `--project-directory`、首个 `-f` 文件所在目录、当前目录决定；`COMPOSE_FILE` 改变项目目录时还可能加载优先级较低的第二份 `.env`。固定部署入口应显式指定文件，避免依赖默认查找。

**采用：**容器内同名变量按来源核对：CLI `docker compose run -e` 优先；官方随后区分来自插值的 `environment`／`env_file`、直接写入的 `environment`、普通 `env_file`、镜像 `ENV`。常规静态配置中 `environment` 会覆盖 `env_file`。宿主已导出的变量仍可能改变插值结果，不能认为 `--env-file` 压过 shell。需要在容器 shell 中展开 `$VAR` 时，Compose 配置使用 `$$VAR` 转义。渲染输出可能包含敏感值，应按配置内容控制可见范围。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/
- https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/
- https://docs.docker.com/reference/compose-file/interpolation/
- https://docs.docker.com/compose/trust-model/

## 5. `local` 日志轮转阈值及容量含义

**采用：**每个常驻服务显式使用以下配置，避免依赖机器全局默认日志驱动：

```yaml
logging:
  driver: local
  options:
    max-size: "20m"
    max-file: "5"
    compress: "true"
```

**已核实：**上述值也是 `local` 官方默认值：单文件达到 `20m` 轮转，最多保留 5 个日志文件，超过时删除最旧文件，默认压缩已轮转文件；官方按每容器约 **100 MB 日志消息**描述默认保留量。`max-size` 接受正整数及 `k/m/g` 单位，`max-file` 是正整数。选项值使用字符串。

**限制：**这是每容器的轮转保留设置，不是整个 Docker 数据目录的严格磁盘配额，也不保证固定保留天数。`local` 捕获容器 stdout/stderr，应用自行写入其他文件的日志不受其管理。Docker 管理日志内部文件，应通过 Docker 日志接口读取，不用外部工具直接修改这些文件。修改 daemon 默认日志配置只作用于之后新建的容器，现有容器不会自动采用新配置；服务现有日志参数的变更应安排重建生效。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/engine/logging/drivers/local/
- https://docs.docker.com/engine/logging/configure/

## 6. socket 的 `:ro` 不等于 API 只读

**采用：**普通业务服务不挂 `/var/run/docker.sock`。确有 Docker 管理需求时，仅专用管理服务持有 socket；将其视为高权限组件，并限制其可接收的操作及调用方。

**限制：**Docker daemon 默认采用“可访问即拥有全部命令权限”的授权模型。socket 挂载成 `:ro` 仍可建立连接、发送创建／删除／挂载等修改 API，不能称为只读 API 或权限沙箱；容器自身 `read_only`、非 root 用户等措施也不能替代 daemon API 授权。OWASP 官方原文明确说明 socket 只读挂载不限制修改请求；Docker 的 Compose 信任模型页虽建议尽量使用只读 socket 挂载，不能据此推导 API 只读。

**采用：**需要细粒度授权时评估 Docker authorization plugin；需要隔离受控工作负载时优先使用专用 daemon／虚拟机，并限制其能访问的宿主数据。后者是依据默认授权能力提出的架构选择，不是单个 Compose 配置字段能提供的保证。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/engine/extend/plugins_authorization/
- https://docs.docker.com/compose/trust-model/
- https://raw.githubusercontent.com/OWASP/CheatSheetSeries/master/cheatsheets/Docker_Security_Cheat_Sheet.md

## 7. 宿主路径采用白名单，但不宣称 Compose 自带路径沙箱

**采用：**配置目录只读挂载，写入数据只挂明确的专用目录或使用命名卷；宿主目录应预先准备。bind mount 用长格式，明确 `read_only` 和 `bind.create_host_path: false`，避免路径拼错时自动创建目录。例如以下是配置片段，路径需替换为实际白名单路径：

```yaml
volumes:
  - type: bind
    source: /absolute/approved/config
    target: /app/config
    read_only: true
    bind:
      create_host_path: false
```

**限制：**bind mount 默认可写，容器可修改或删除对应宿主文件。源路径是 **daemon 所在宿主**的路径，不是 Docker CLI 所在机器的路径；Docker Desktop 的 daemon 在 Linux VM 中，通过 Desktop 的共享机制访问原生宿主路径。不要把本机原生路径与 VM 内路径视为完全相同的安全边界。递归只读涉及内核能力：Linux 内核低于 5.12 时，顶层只读不保证子挂载只读。

**安全边界：**Compose 项目目录不是文件访问沙箱；`env_file`、`configs.file`、`secrets.file`、`include` 等可读取执行 Compose 的用户有权读取的文件，也可能经符号链接越出项目目录；仅渲染 `config` 也可能读出并显示文件内容。`create_host_path: false` 只是不自动建目录，不负责限制允许访问的宿主范围。

**设计判断：**若业务 API 接收宿主路径，必须自行实施规范化后的路径白名单、限制可控的源路径及挂载选项，并保护白名单目录本身；不能只检查字符串前缀或依赖工作目录。即使实现这些检查，也不能将其视为恶意代码的强隔离边界：持有 daemon socket 的服务仍有能力请求其他挂载。强隔离应在专用 daemon／虚拟机及其可见宿主目录边界落实。

来源（核实日期：2026-11-13）：
- https://docs.docker.com/engine/storage/bind-mounts/
- https://docs.docker.com/reference/compose-file/services/#volumes
- https://docs.docker.com/compose/trust-model/
- https://docs.docker.com/engine/extend/plugins_authorization/

## 采用清单与研究边界

建议基线：显式双 Compose 文件与 `--env-file`；使用 `!override` 时 Compose ≥ 2.24.4；关键依赖等待健康；常驻服务退出重启、应用负责断线恢复；各服务 `local: 20m × 5`；socket 权限集中；宿主挂载白名单、只读优先、禁止自动建目录。以上选择及限制以各节列出的原始来源为依据，统一核实日期为 **2026-11-13**。

本次只做官方资料核实和此文档写入，未评审现有业务实现，未执行 Docker 命令或改变容器状态。网页工具未返回可读结果时，使用只读 HTTP 请求获取官方原页正文交叉核实。
