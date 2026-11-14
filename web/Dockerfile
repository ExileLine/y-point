# 1. Node 用于编译前端，Nginx 用于提供编译后的页面。
ARG NODE_IMAGE=node:24-alpine
ARG NGINX_IMAGE=nginx:1.28-alpine

# 2. 构建阶段：先根据锁文件安装依赖，再复制源码，便于复用依赖安装缓存。
# (1) 以 Node 镜像为基础，把这个阶段命名为 build。
# (2) 将容器中的工作目录设为 /web。
# (3) 复制依赖清单和锁文件。
# (4) 按锁文件安装依赖。
# npm ci 用于按照 package-lock.json 安装依赖；缓存挂载帮助复用已经下载过的包。--no-audit --no-fund 则跳过安装时的漏洞审计请求和赞助提示。
FROM ${NODE_IMAGE} AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund

# 3. 复制前端源码和配置，执行 TypeScript 检查与 Vite 构建，生成 /web/dist。
# 这里先复制依赖清单，再复制源码，是为了方便复用构建缓存：只修改页面源码时，有机会直接复用之前的依赖安装结果。
COPY web/index.html web/tsconfig*.json web/vite.config.ts ./
COPY web/src ./src
COPY web/public ./public
RUN npm run build

# 4. 运行阶段：只复制编译产物和 Nginx 配置、启动脚本。
# 最终镜像以 Nginx 为基础，从 build 阶段取出编译好的页面，再放入项目的 Nginx 配置和启动脚本。
# 默认使用普通用户运行。
# 声明容器内使用 8080 和 8443 两个端口。
# 启动时执行 start.sh，由它生成配置并启动 Nginx。
FROM ${NGINX_IMAGE} AS runtime
COPY --from=build /web/dist /usr/share/nginx/html
COPY deploy/nginx/ /opt/y-point/

# 5. 默认使用普通用户；启动脚本生成 Nginx 配置后在前台运行服务。
# 8080 和 8443 是容器内部端口，宿主机端口由 Compose 配置。
# cd /y-point
# docker build -f web/Dockerfile -t y-point-web:local .
# -f web/Dockerfile：指定使用前端的 Dockerfile
# . 仍然表示项目根目录，因为构建过程还需要读取根目录下的 deploy/nginx/ 文件。
USER 10001:10001
EXPOSE 8080 8443
ENTRYPOINT ["/bin/sh", "/opt/y-point/start.sh"]
