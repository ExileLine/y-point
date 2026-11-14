# 1. 基础镜像：Python 运行环境、uv 依赖管理工具和 Docker 客户端。
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.16
ARG DOCKER_CLI_IMAGE=docker:29-cli

# 2. 为工具镜像命名，后面只复制需要的可执行文件。
FROM ${UV_IMAGE} AS uv
FROM ${DOCKER_CLI_IMAGE} AS docker-cli

# 3. 依赖构建阶段：依据锁文件安装生产依赖，生成 /app/.venv。
# (1) 准备 Python。
# (2) 安装 uv。
# (3) 进入 /app 工作目录。
# (4) 放入依赖清单和锁文件。
# (5) 按锁文件安装运行所需的依赖，生成 /app/.venv。
# --locked：按照 uv.lock 安装，锁文件与依赖声明不一致时直接报错。
# --no-dev：不安装 pytest 等开发依赖。
# --no-install-project：当前项目按源码运行，这里只安装它需要的依赖。
# type=cache：复用下载缓存，加快后续构建
FROM ${PYTHON_IMAGE} AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project

# 4. 最终运行阶段：从新的 Python 基础镜像开始，安装文档处理所需的系统组件。
# ca-certificates：访问 HTTPS 服务时验证证书
# libstdc++6：支持 PDFium 等原生组件运行
# fontconfig、fonts-noto-cjk：提供中文字体支持
# tesseract-ocr和语言包：识别图片中的中英文文字
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="Y-Point API and worker" \
      org.opencontainers.image.authors="yangyuexiong"
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=3 \
    && apt-get install -y --no-install-recommends -o Acquire::Retries=3 \
      ca-certificates libstdc++6 fontconfig fonts-noto-cjk \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ypoint \
    && useradd --uid 10001 --gid 10001 --no-create-home ypoint

# 5. 复制已安装的 Python 依赖、Docker 客户端和项目运行文件。
# Docker 客户端供脚本沙箱调用；Docker 引擎的连接权限由部署配置另行提供。
# 从前面的阶段取出 Python 依赖和 Docker 客户端。
# 放入后端代码、数据库迁移、沙箱和部署脚本。
COPY --from=dependencies /app/.venv /app/.venv
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY app ./app
COPY migrations ./migrations
COPY sandbox ./sandbox
COPY deploy ./deploy
COPY alembic.ini ./

# 6. 使用虚拟环境中的命令，及时输出日志，并以普通用户运行。
# PATH：让 python、uvicorn 使用镜像内的虚拟环境。
# PYTHONUNBUFFERED=1：及时输出 Python 日志，便于查看。
# USER：指定默认运行用户。
# EXPOSE 8000：声明服务使用的容器端口；宿主机如何访问它，要在启动配置中决定。
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp
USER 10001:10001
EXPOSE 8000

# 7. 启动时先检查部署配置，再执行服务命令；默认启动 API，也可覆盖为 worker 或迁移命令。
# 在本项目中，启动顺序是：
# 启动容器
#   ↓
# entrypoint.sh 检查部署配置
#   ↓
# 启动 Uvicorn
#   ↓
# 提供后端 API 服务
# 默认启动一个 Uvicorn 进程，监听容器内的 8000 端口。同一个镜像后面也会用于启动 worker，通过替换启动命令来决定它承担哪个角色。
# 在项目根目录执行：
# cd /y-point
# docker build -t y-point-server:local .
# docker build：根据 Dockerfile 构建镜像
# -t y-point-server:local：给镜像取名并设置标签`local`;`test`;`prod`;...
# .  使用当前目录作为构建上下文
ENTRYPOINT ["/bin/sh", "/app/deploy/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
