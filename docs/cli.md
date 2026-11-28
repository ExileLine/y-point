# 统一命令行与项目配置

[文档总览](README.md) · [本地开发](local-development.md) · [部署手册](deployment-guide.md)

`ypoint_cli` 集中实现开发启动、Docker 部署和检查，不导入 FastAPI、模型 SDK 或数据库配置。CLI 操作的是源码仓库中的 Compose、配置模板和前端，因此需要完整源码；安装一个 wheel 不会自动携带部署资源。

## 安装与入口

开发环境在项目根目录执行：

```bash
uv sync --locked
uv run yp --help
uv run yp dev
uv run yp deploy --sandbox
```

`uv sync` 安装依赖及项目本身，并在 `.venv/bin` 生成 `yp`。`uv run` 使用项目环境运行命令；直接运行 `yp` 需要先激活环境或使用 `.venv/bin/yp`。`--locked` 要求清单与锁文件一致，不一致时失败，不静默更新依赖锁定结果。

纯 Docker 宿主机不必安装应用依赖，在仓库根目录使用相同 CLI 的标准库入口：

```bash
python3 -m ypoint_cli --help
python3 -m ypoint_cli deploy --sandbox
```

开发模式仍需要 uv、应用依赖、Node.js 和前端依赖。部署需要 Python 3.12+、已启动的 Docker Engine/Desktop 和 Compose 插件。`uv run yp deploy` 可能先同步应用依赖；希望保持宿主机轻量时使用模块入口。

已安装 `yp` 时，从其他目录操作可以显式指定源码仓库：

```bash
yp --project-dir /path/to/y-point deploy --sandbox
```

`--project-dir` 放在子命令之前。省略时依次从当前目录及 CLI 安装位置向上查找源码。`--directory` 和证书等显式相对路径相对于调用时的工作目录；默认部署目录相对于找到的源码根目录。帮助和版本查询不需要 Docker 或有效部署配置。

开发、部署和检查命令统一由 `ypoint_cli/` 实现。下面各列是同一操作的等价入口，每行选择一种执行即可；子命令和参数保持一致。`uv run yp` 在项目根目录执行，`yp` 列需要已安装项目并激活虚拟环境，模块入口在源码根目录执行。开发服务的依赖与配置准备见[本地开发](local-development.md)。

| 操作 | 使用 uv | 已激活项目虚拟环境 | Python 标准库入口 |
| --- | --- | --- | --- |
| 首次部署并创建管理员 | `uv run yp deploy --sandbox --create-admin admin` | `yp deploy --sandbox --create-admin admin` | `python3 -m ypoint_cli deploy --sandbox --create-admin admin` |
| 后续更新并保留原协议 | `uv run yp deploy --sandbox` | `yp deploy --sandbox` | `python3 -m ypoint_cli deploy --sandbox` |
| 显式使用 HTTP 部署 | `uv run yp deploy --sandbox --http` | `yp deploy --sandbox --http` | `python3 -m ypoint_cli deploy --sandbox --http` |
| 启动开发服务 | `uv run yp dev` | `yp dev` | `python3 -m ypoint_cli dev` |
| 仅初始化配置 | `uv run yp init --http` | `yp init --http` | `python3 -m ypoint_cli init --http` |
| 查看 worker 日志 | `uv run yp compose --sandbox -- logs --follow --tail 100 worker` | `yp compose --sandbox -- logs --follow --tail 100 worker` | `python3 -m ypoint_cli compose --sandbox -- logs --follow --tail 100 worker` |
| 检查已有部署 | `uv run yp verify-deployment --sandbox` | `yp verify-deployment --sandbox` | `python3 -m ypoint_cli verify-deployment --sandbox` |
| 查看部署帮助 | `uv run yp deploy --help` | `yp deploy --help` | `python3 -m ypoint_cli deploy --help` |

表中部署命令默认选择 `local`；操作其他环境时，在相应子命令后继续传入 `--environment production` 或 `--environment staging`。`compose` 的 `--` 分隔符、`--sandbox` 以及已有的目录、协议、监听地址等参数在三种入口下都应保留。更换入口不会改变部署环境或配置，也不表示需要再执行一次部署。

## 子命令与公共参数

| 命令或参数 | 含义 |
| --- | --- |
| `yp dev` | 管理 API、Worker、Web 开发进程。API 使用 7769；Web 从 5173 开始选择可用端口。Ctrl-C 清理本次服务，重复启动接管同项目管理锁下的旧服务。 |
| `yp deploy` | 检查 Docker → 初始化或复用配置 → 构建镜像 → 启动并等待健康 → 显示状态，可按需创建管理员。 |
| `yp init` | 只初始化或更新私有部署配置，不启动服务，不自动重建镜像。 |
| `yp compose` | 根据所选环境、已保存协议及显式沙箱开关合并 Compose 文件，执行分隔符后的 Docker Compose 命令；省略命令时执行 `ps`。 |
| `yp verify-deployment` | 检查已有部署的页面、健康、Redis、迁移、日志与运行依赖，按选项检查沙箱或重建 API。不会发起模型对话。 |
| `--project-dir PATH` | 源码根目录，放在子命令之前；不是数据目录。 |
| `--version` | 显示安装包版本；源码未安装时使用当前源码版本。 |
| `-h` / `--help` | 查看总帮助；`yp <子命令> --help` 查看该命令参数。 |
| `--` | `compose` 的参数边界，其后的参数原样交给 Docker Compose，不再作为 yp 的部署选项解析。 |

环境选择不依赖机器名：`local` 为本机、`staging` 为预发布、`production` 为生产；默认 `local`。local 容器中的应用仍使用关闭 DEBUG 的 production 配置，环境名不等于应用调试模式。

## 一键部署参数

示例：

```bash
uv run yp deploy --sandbox --create-admin admin
uv run yp deploy --environment production --sandbox --http --bind-ip 0.0.0.0 --http-port 18080
uv run yp compose --environment production --sandbox -- logs --follow --tail 100 worker
```

| 参数 | 默认与作用 |
| --- | --- |
| `--environment {local,staging,production}` | 默认 `local`，选择配置隔离目录、Compose 项目及生产资源覆盖。 |
| `--directory PATH` | 默认 `<源码根目录>/.deployment/<environment>`，保存私有环境文件、证书与数据。已有目录必须匹配环境。 |
| `--bind-ip IPv4` | 新部署默认 `127.0.0.1`；显式传入时更新 Web 宿主监听地址。内网/服务器可指定可达 IP；`0.0.0.0` 监听所有 IPv4 网卡。 |
| `--http-port PORT` | 新部署默认 `18080`；显式传入更新已保存的 HTTP 端口，范围 1–65535。 |
| `--https-port PORT` | 新部署默认 `18443`；启用 HTTPS 时必须有效且不同于 HTTP 端口。 |
| `--http` | 显式采用 HTTP，不需要证书；与 `--https` 互斥。 |
| `--https` | 显式采用 HTTPS；local 可以生成自签证书，其他环境需要正式证书。 |
| `--tls-cert PATH` / `--tls-key PATH` | 配对提供证书和私钥，单独传一项会报错；不能与 `--http` 同时使用。提供证书会启用 HTTPS。 |
| `--sandbox` | 接入受信任的本机 Docker 并准备脚本沙箱镜像，供 Python/Bash Tool、Skill 脚本和 MCP stdio 使用。不是应用聊天沙箱；不会自动记忆，后续部署和 Compose 操作继续传入。 |
| `--skip-build` | 跳过应用及沙箱镜像构建，使用已有镜像；不是跳过初始化、迁移或健康检查。 |
| `--wait-timeout SECONDS` | 默认 `180`，正整数；Compose 启动时等待健康检查的时限，不控制模型或工具超时。 |
| `--create-admin USERNAME` | 服务就绪后交互创建管理员。只传用户名，密码在终端输入；非交互环境拒绝此选项。已有管理员的更新不需要它。 |

省略协议、监听地址和端口选项时，已有配置保持原值；新部署默认 HTTP。初始化保留原数据库、JWT 和供应商加密密钥；配置缺损时报告错误，不自动生成替代密钥。失败保留配置、数据及容器，命令返回失败退出码。

## 初始化、Compose 与验证参数

`yp init` 使用上表的环境、目录、绑定 IP、端口、协议及证书参数，另外支持：

| 参数 | 含义 |
| --- | --- |
| `--uid UID` / `--gid GID` | 初始化容器运行及私有目录使用的用户/组 ID。新配置默认使用当前非 root 用户；root 下默认使用 10001，已有配置保留原值。 |
| `--docker-socket PATH` | 新配置的宿主 Docker socket 路径，默认 `/var/run/docker.sock`。 |
| `--docker-gid GID` | 新配置使用的 Docker socket 组 ID，未提供时从存在的 socket 获取，不存在时使用 0。 |
| `--sandbox` | 接受该标记，但初始化不会自动启用后续调用的沙箱覆盖；是否接入 Docker 由每次 `deploy` / `compose` 的 `--sandbox` 决定。 |

`yp compose` 使用 `--environment`、`--directory`、`--sandbox`；协议和端口读取已有配置，不在 Compose 调用中修改。`--` 后可以使用 Docker Compose 自身的 `ps`、`logs`、`build`、`up`、`exec`、`down` 等命令与参数。`down` 会停止服务；卷或数据删除行为由传入的 Docker 参数决定。

`yp verify-deployment` 支持：

| 参数 | 含义 |
| --- | --- |
| `--environment` / `--directory` | 指定已有部署，默认检查 local 的私有目录。 |
| `--sandbox` | 使用沙箱覆盖并验证 API/Worker 的真实 Docker 脚本及受信任代理能力。 |
| `--restart` | 显式重建 API，检查 Nginx 重新发现及文件持久化；只在无活跃任务的隔离环境使用。默认不重建。 |

## pyproject.toml 逐项说明

`[xxx]` 是 TOML 的配置表，不是命令行参数。`project`、`build-system` 使用 Python 打包标准；`tool` 下的表由对应工具解释。

| 配置 | 本项目含义 |
| --- | --- |
| `[project]` | 项目标准元数据与运行依赖。 |
| `name = "y-point"` | Python 安装包的发行名称；不同于导入包名 `app`、`ypoint_cli`，也不同于可执行命令名 `yp`。 |
| `version = "0.1.0"` | 安装包版本，安装后 `yp --version` 从发行元数据读取。发布时与源码回退版本一起更新。 |
| `description` | 项目的简短描述。 |
| `readme = "README.md"` | 打包时作为项目长说明读取的文件，不表示 README 中的命令会被执行。 |
| `requires-python = ">=3.12"` | 声明允许使用的 Python 版本，不是依赖安装源或自动升级命令。 |
| `dependencies` | API、Worker 和模型/文档处理所需的第三方运行依赖。`==` 固定声明版本，`>=` 是下界，`<` 是上界，逗号表示同时满足；最终解析结果由 `uv.lock` 固定。 |
| `[project.scripts]` | 标准 console script 声明。项目安装时生成命令包装器。 |
| `yp = "ypoint_cli.main:main"` | 生成名为 `yp` 的命令；导入 `ypoint_cli.main` 模块并调用其 `main()`，返回整数作为退出码。不是把函数名写成 shell 命令。 |
| `[dependency-groups]` | 单独声明开发/测试工具组。默认 uv 同步包含 dev 组；`--no-dev` 排除它，`--group dev` 显式包含它。 |
| `dev = ["pytest>=8.3.2"]` | 测试依赖，不属于 `[project.dependencies]` 的应用运行依赖。 |
| `[tool.uv]` | uv 的专属配置命名空间，其他工具不一定读取。 |
| `package = true` | uv 将当前项目视为可安装包，常规同步会安装项目自身，从而生成 `yp`。只写 `[project.scripts]` 而继续 `package = false` 不会安装该命令。 |
| `[build-system]` | 声明构建 wheel 或可编辑安装时使用的工具，不是 Docker 镜像构建配置。 |
| `requires = ["setuptools>=68.0"]` | Python 构建环境需要的依赖，与应用运行依赖分别管理。 |
| `build-backend = "setuptools.build_meta"` | 选择 setuptools 的标准构建后端。 |
| `[tool.setuptools.packages.find]` | 明确发现哪些 Python 包，避免平铺仓库中的 docs、deploy、sandbox 等目录被错误打包。 |
| `include = ["app", "app.*", "ypoint_cli", "ypoint_cli.*"]` | 只包含应用包、CLI 包及它们的子包。不会将私有部署数据作为包自动纳入。 |
| `namespaces = false` | 只发现包含 `__init__.py` 的普通 Python 包，不自动把普通目录视为命名空间包。 |
| `[tool.ruff]` / `line-length = 160` | Ruff 的行宽设置，为检查与格式化提供参考，不限制页面或模型输入长度。 |
| `[tool.ruff.lint]` / `select` | 启用 E4、E7、E9、F、E501 规则族：导入/语句与语法类错误、Pyflakes 检查及超长行检查。 |
| `[tool.ruff.format]` / `skip-magic-trailing-comma = false` | 格式化时保留尾逗号触发多行布局的行为。 |

Dockerfile 的依赖层继续使用 `uv sync --locked --no-dev --no-install-project`：安装锁定的生产依赖，排除开发组，并跳过安装当前项目。最终镜像复制 `app` 源码运行 API/Worker，不需要宿主部署 CLI。该选项是对本次同步的显式控制，不与 `package = true` 冲突。

项目声明范围变化后执行 `uv lock` 并提交清单和锁文件；常规开发与构建使用 `--locked` 检查一致性。本项目根包启用安装不会自行升级既有第三方依赖。


配置格式依据：[Python 项目元数据与命令入口](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)、[uv 项目配置](https://docs.astral.sh/uv/concepts/projects/config/)、[setuptools 包发现](https://setuptools.pypa.io/en/latest/userguide/package_discovery.html)。本项目的命令与默认值以源码和 `--help` 为准。
