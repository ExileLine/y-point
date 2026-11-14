# HTTP 与可选 HTTPS 验证记录（2026-11-14）

本记录针对新部署默认 HTTP、按需启用 HTTPS 的变更。2026-11-13 的 HTTPS 基线记录保留，不用本轮结果替代目标服务器或 M8/M11 总验收。当前使用步骤见 [deployment.md](deployment.md)。

## 实现与兼容

- 基础 Compose 只提供 HTTP；新增 `docker-compose.https.yml` 增加 TLS 证书挂载及 HTTPS 端口，由部署入口根据保存的 `YPOINT_HTTPS_ENABLED` 自动加载。
- `init` 新建默认 HTTP，`--http` / `--https` 显式选择协议；成对证书参数兼容旧命令。HTTP 不要求证书和 OpenSSL，只有 local HTTPS 缺证书时生成 30 天自签证书。
- 无开关的旧配置继续 HTTPS；显式切换同步 `STREAM_COOKIE_SECURE`，不轮换密码、JWT/供应商密钥或业务数据。外层网关终止 TLS 时可以手动保留 Secure Cookie。
- HTTP 和 HTTPS 使用相同的 Nginx 页面/API/SSE 配置。前端补充非安全 HTTP 浏览器 UUID 及复制兼容。

## 自动化证据

| 检查 | 实际结果 | 覆盖范围 |
| --- | --- | --- |
| `uv run pytest -q tests/test_deployment.py tests/test_run_api.py` | 18 passed, 3 warnings，8.93 秒 | 所有环境无证书/无 OpenSSL 初始化、秘密保留、缺损配置拒绝、协议切换、旧配置、生产覆盖、端口冲突、HTTP/HTTPS Cookie 与 SSE 授权流程。 |
| `YPOINT_TEST_DEPLOYMENT_PROXY=1 uv run pytest -q tests/test_deployment_proxy.py --tb=short` | 2 passed, 3 warnings，4.61 秒 | 实际 Nginx 镜像、HTTP 无证书挂载、HTTPS 证书校验、SPA、延时 SSE、Cookie 转发与请求 ID。 |
| `npm --prefix web run build` | 通过 | TypeScript 与 Vite 生产构建；保留已有大 chunk 提示。 |
| 修改 Python 文件 Ruff 检查与格式、`git diff --check` | 通过 | 7 个 Python 文件；当前旧 HTTPS 配置 `config --quiet` 及四个常驻容器健康检查也通过。 |
| 后端与 Web Docker 镜像构建 | 通过 | 后端 production Cookie 设置与 Web 可选 TLS 配置/前端兼容代码。 |
| `npm --prefix web run verify:http` | 9 项断言通过 | 非安全 HTTP 来源，原生 randomUUID/clipboard 确实不可用；1000 个 UUID 格式和唯一性、真实点击复制、临时节点清理与焦点恢复。 |
| `WEB_HTTP_COMPAT=1 npm --prefix web run verify:uploads` | 52 项断言通过 | 桌面 1280px、手机 390px；真实生产页面的模拟 API 上传/预检/发送失败恢复与 request_key 提交。 |

Python warnings 为既有 pytest 历史临时目录清理问题，不是业务断言失败。Nginx 测试上游探针启动时先等待 socket 就绪，避免健康入口与探针监听之间的测试竞态。

## 独立 HTTP Compose 部署

本机 Docker Desktop 使用项目 `y-point-http-verify-5ae0182c`、独立随机私有目录、端口 49368 与独立 PostgreSQL 卷；全程无 TLS 目录或证书挂载。执行全新初始化和 `up -d --wait --wait-timeout 180` 后，db/api/worker/web 健康，migrate 正常完成。

`scripts/verify_deployment.py --directory <临时目录>` 通过 HTTP 页面/SPA、API、worker、错误路由、日志 local 驱动与轮转参数、Alembic 结构检查、worker 专属心跳及镜像内 PDF/OCR 依赖核对。两个浏览器脚本均访问 `http://ypoint-http.test:49368`，通过 Chromium host resolver 映射至本机，并断言 `isSecureContext === false`，避免 localhost 的安全上下文例外掩盖问题。

验证完成后，仅删除上述临时项目的容器、网络、命名卷与临时私有目录。没有更改 `.deployment/local`、既有 `y-point-local` 容器、业务库或账号。原本运行中的 HTTPS 部署继续使用原容器；镜像重建不代表已将原部署更新为 HTTP。

## 未覆盖的验收

本轮没有真实外部模型调用，不建立正式管理员账号，不重放业务任务；模拟 API 的浏览器结果不替代真实模型 SSE 与完整登录/文件业务。未部署目标服务器或验证正式域名/证书、跨主机网关、容量、容灾及发布制品扫描；这些仍按实际发布范围独立验收。
