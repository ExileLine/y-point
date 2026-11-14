#!/bin/sh
set -eu

# 根据部署开关选择监听方式；HTTP 模式完全不读取证书。
case "${YPOINT_HTTPS_ENABLED:-false}" in
  true)
    case "${YPOINT_HTTPS_PORT:-18443}" in
      *[!0-9]*|'') echo 'YPOINT_HTTPS_PORT 必须是端口数字' >&2; exit 1 ;;
    esac
    export YPOINT_HTTPS_PORT="${YPOINT_HTTPS_PORT:-18443}"
    test -r /run/tls/tls.crt && test -r /run/tls/tls.key || {
      echo 'HTTPS 已启用，但 TLS 证书或私钥不可读' >&2; exit 1;
    }
    server_template=/opt/y-point/https.conf.template
    ;;
  false) server_template=/opt/y-point/http.conf.template ;;
  *) echo 'YPOINT_HTTPS_ENABLED 仅支持 true 或 false' >&2; exit 1 ;;
esac

envsubst '${YPOINT_HTTPS_PORT}' < "$server_template" > /tmp/nginx-servers.conf
cp /opt/y-point/nginx.conf.template /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g 'daemon off;'

# 启用 HTTPS 时，读取 HTTPS 对外端口：为空时，采用 18443。
# 然后，生成真正的 Nginx 配置文件：envsubst...
# 读取所选协议模板，仅替换 ${YPOINT_HTTPS_PORT}，生成服务器配置。
# -c：使用刚生成的配置文件。
# daemon off;：让 Nginx 在前台运行。
# exec：和后端脚本一样，用服务程序替换当前 Shell 进程。
