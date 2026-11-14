# Docker 部署逐步讲解

按构建到运行的顺序理解部署文件；直接执行部署请使用部署手册。

[文档总览](README.md) · [项目首页](../README.md)

## Docker 部署（API、worker、前端与 PostgreSQL）

完整操作步骤见 **[部署手册](deployment-guide.md)**，覆盖本机模拟、内网服务器、可选 HTTPS、管理员创建、日志、数据、更新和排查。设计与配置细节见 [部署架构与配置参考](deployment.md)。

在项目根目录执行，一键完成 Docker 检查、初始化、配置校验、镜像构建、启动和健康等待：

```bash
# 本机模拟生产，新配置默认 HTTP：http://127.0.0.1:18080。
python3 scripts/deploy_all.py

# 首次内网服务器部署；按实际访问范围选择监听 IP，密码在终端输入。
python3 scripts/deploy_all.py --environment production --http \
  --bind-ip 0.0.0.0 --http-port 18080 --create-admin admin
```

服务器访问 `http://服务器内网IP:18080`。新部署无需域名和证书；HTTPS 按需用 `--https` 开启，staging/production 首次需成对提供证书/私钥。已有配置默认保留原协议和密钥，重复部署不重复创建管理员；本机脚本沙箱通过 `--sandbox` 显式启用。所有参数见 `python3 scripts/deploy_all.py --help`。

### 部署文件与执行过程

以下按构建到运行的顺序解释文件职责。操作时可对照 [手动部署步骤](deployment-guide.md#3-手动部署理解一键脚本执行的步骤)。

1. **先构建后端镜像：根 `Dockerfile`。** 多阶段构建先用 uv 按 `uv.lock` 安装依赖，再把依赖和代码复制到运行镜像。`FROM` 选择基础镜像，`WORKDIR` 设置容器内目录，`COPY` 复制构建上下文里的文件，`RUN` 在构建时执行命令；`USER` 指定运行身份，`ENTRYPOINT` 指定启动入口，`CMD` 提供默认服务命令。API 和 worker 共用一个镜像，通过不同启动命令区分职责。
2. **理解后端启动：`deploy/entrypoint.sh`。** 容器启动时先运行 `preflight.py` 核对密钥、目录权限及启用时的沙箱条件，再通过 `exec "$@"` 运行服务，使退出信号交给主进程。一次性数据库迁移由独立 migrate 服务完成，不塞进每个 API 启动过程。
3. **再构建前端镜像：`web/Dockerfile`。** Node 阶段执行 TypeScript/Vite 生产构建，Nginx 阶段只带静态页面和网关配置；运行时不启动 Vite。前后端默认在同一主机的不同容器中，镜像可独立构建和升级；跨主机 upstream、共享文件与可信代理需要额外配置。
4. **理解 Nginx 启动：`deploy/nginx/start.sh`。** 根据保存的协议选择 HTTP 或 HTTPS 模板，生成配置后前台启动 Nginx。`locations.conf` 共用页面回退、缓存、API、SSE、上传限制及请求 ID 规则。HTTP 不读取证书；只有可选 HTTPS 配置才挂载证书并启用 HTTP 跳转。
5. **编排服务：`docker-compose.yml`。** PostgreSQL 健康后启动一次性迁移，迁移成功后启动 API/worker，API 健康后启动 Web。只有 Web 发布宿主端口，数据库与 API 留在内部网络。`ports` 左边是宿主端口、右边是容器端口；`mem_limit` 是每容器内存上限，`cpus` 限制可使用的 CPU 时间额度，例如 `1.0` 约为一颗 CPU 的持续计算额度，不表示固定绑到某个核心。
6. **给容器补运行约束。** 非 root、只读根目录、tmpfs、健康检查、资源上限和重启策略由 Compose 配置。`init: true` 负责回收子进程；API 直接运行 Uvicorn，后台任务由独立 worker 执行，不再引入 supervisor。Uvicorn 的 `--workers 1` 是当前默认，可按负载调整；多 API 进程不会增加后台任务 worker 数量，扩容前仍需压测与资源核对。
7. **持久化数据。** `postgres-data` 是 Docker 命名卷，不是项目目录；普通 Linux rootful Docker 常见于 `/var/lib/docker/volumes/<项目名>_postgres-data/_data`，实际用 `docker volume inspect` 查询，Docker Desktop 位于 VM 内。上传文件和脚本目录通过本环境的 `data/` 绑定挂载。普通 `down` 保留数据，`down -v` 会删除数据库卷。
8. **选择环境并初始化：`scripts/deploy.py init`。** `--environment` 明确选择 local/staging/production，省略为 local，不自动猜测机器环境。每个环境生成独立 `compose.env`、`application.env`、`database.env`、密钥和数据目录；HTTPS 才需要 `tls/`。local 也使用关闭 DEBUG 的 production 应用配置。重复初始化保留密码和密钥，显式传入协议、监听地址或端口时更新对应字段。
9. **合并配置并启动：`scripts/deploy.py compose`。** 脚本用多个 `-f` 按顺序组合基础配置、production 资源覆盖、可选 HTTPS 和显式沙箱覆盖，再透传 build/up/logs 等子命令。协议从 `compose.env` 读取；`--sandbox` 只启用本机 Docker 接入，用户脚本容器在调用时临时创建。启用沙箱后，后续 compose/一键部署命令继续带该参数。
10. **管理镜像和日志。** `:local` 是默认值，初始化按环境生成镜像标签；可以在 `compose.env` 修改 `YPOINT_SERVER_IMAGE` / `YPOINT_WEB_IMAGE`。标准流由 Docker `local` 驱动按每容器 `20m × 5` 轮转压缩；这不限制整个磁盘。Nginx 请求 ID 对应 API 的 `rid`，Run ID 关联后台执行，业务审计保存在数据库；更新前需要备份数据库、文件和同一套密钥。

一键脚本复用上述流程。手动执行的主线是 `init → config --quiet → build → up --wait → 首次创建管理员`，完整操作以 [部署手册](deployment-guide.md) 为准。基础镜像包含 PDFium、Tesseract 中英文语言包和中文字体；迁移/升级、沙箱权限及日志保留边界见 [配置参考](deployment.md)。部署就绪不代表真实模型、容量或目标环境总验收完成。
