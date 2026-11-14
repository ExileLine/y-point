# 本地开发

准备开发环境并运行 API、worker 和前端。

[文档总览](README.md) · [项目首页](../README.md)

## 本地运行

使用 Python 3.12。`uv` 从锁文件安装依赖；默认读取 `.env.development`，可用不提交到 Git 的 `.env.development.local` 覆盖本机配置。`.env.example` 是配置项说明，不是可直接启动的环境文件。开发环境默认使用项目根目录的 SQLite `y_point.db`，也可通过 `DB_BACKEND` 选择 MySQL 或 PostgreSQL。

开发需要 Python 3.12、uv、Node.js 和 npm；推荐 Node.js 24，与当前前端 Docker 构建版本一致。开发启动器使用 POSIX 文件锁与进程接口，建议在 Linux/macOS 环境运行。项目根目录先安装依赖：

```bash
uv sync --group dev
npm --prefix web ci
```

首次开发时创建仅本机使用的密钥配置；下面的命令以独占方式创建文件，已有 `.env.development.local` 时会拒绝覆盖，请改为检查并补齐原文件中的配置：

```bash
uv run python - <<'PY'
import secrets
from pathlib import Path
from cryptography.fernet import Fernet

with Path(".env.development.local").open("x", encoding="utf-8") as config:
    config.write(f"SECRET_KEY={secrets.token_urlsafe(48)}\n")
    config.write(f"CREDENTIAL_ENCRYPTION_KEY={Fernet.generate_key().decode()}\n")
    config.write("REDIS_ENABLED=False\n")
Path(".env.development.local").chmod(0o600)
PY
```

升级开发数据库，首次创建管理员，再运行 `make dev` 同时启动 API、worker 和前端。已有管理员时省略创建命令：

```bash
uv run alembic upgrade head
uv run python -m app.cli.create_admin admin
make dev
```

前端默认地址为 `http://127.0.0.1:5173`；若端口被占用，`make dev` 会选择下一个空闲端口并打印实际地址。API 地址为 `http://127.0.0.1:7769`，前端 `/api/v1` 请求会代理到该 API。重复运行 `make dev` 会先停止本项目旧的 API、worker 和前端开发进程，待其退出后再启动新进程；按 Ctrl-C 也会清理本轮进程。若 7769 被其他程序占用，脚本仍会报错，不会终止该程序。独立运行前端时仍默认代理到 8000，可通过 `API_PROXY_TARGET` 指定其他 API 地址。

只需后端时，可单独启动 API 和 worker：

```bash
uv sync --group dev
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

对话运行还需要单独启动 worker；API 进程只接收请求并读取结果。SQLite 检查点默认保存在 `.run-private/checkpoints.sqlite`，重启 worker 时应继续使用原文件，以保留已有会话上下文。

Worker 的标准输出、错误输出及继承标准流的子进程输出统一写入项目根目录的 `.logs/worker.log`，因此 `make dev` 的终端只显示 API 和前端日志。日志默认限制为 50 MiB（52,428,800 字节），超限时在原文件中清理最早内容，通常保留约 45 MiB 的最新日志后继续写入，不生成备份日志；启动时也会收缩已有超限文件。可用 `tail -f .logs/worker.log` 查看执行日志。生产环境可在 `.env.production` 或进程环境中设置 `WORKER_LOG_FILE=/path/to/worker.log`、`WORKER_LOG_MAX_BYTES=52428800`；容量必须为正整数字节数，相对路径按项目根目录解析，目录会自动创建。修改后重启 worker 生效，`make dev` 的正常停止流程会刷新并收尾日志。

```bash
uv run python -m app.cli.run_worker
uv run pytest -q
uv run alembic check
```

`GET /api/v1/health` 只检查 API；根入口与该接口统一返回 `{"code":200,"message":"操作成功","data":{"status":"ok"}}`，健康探针读取 `data.status`。`GET /api/v1/health/worker` 在有有效 worker 心跳时返回 HTTP 200，否则返回 503。Worker 停止时新 Run 仍可进入队列，但不会执行。管理员可从 `GET /api/v1/runs/workers/status` 查看心跳、排队量和过期租约。开发接口文档位于 `/docs`。Redis 由 `REDIS_ENABLED` 控制，本地启动不依赖 Redis。

开发环境的 `SECRET_KEY` 应通过私有配置覆盖占位值，至少使用 32 字节随机值；修改后重启服务，旧访问令牌会失效。供应商凭据使用独立的 Fernet `CREDENTIAL_ENCRYPTION_KEY` 加密，丢失或更换该密钥后已有凭据无法解密。生成密钥可运行：

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
