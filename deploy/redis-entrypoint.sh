#!/bin/sh
# 使用镜像内的 redis 用户运行；口令仅写入 tmpfs 配置，不放入进程命令参数。
set -eu
umask 077

: "${REDISCLI_AUTH:?缺少 Redis 密码，请先初始化部署配置}"
case "$REDISCLI_AUTH" in
    *[!a-zA-Z0-9_-]*) echo "Redis 密码格式不受支持" >&2; exit 1 ;;
esac
case "${REDIS_MAXMEMORY:-256mb}" in
    *[!a-zA-Z0-9]*) echo "Redis 内存配置格式不受支持" >&2; exit 1 ;;
esac

cat > /tmp/y-point-redis.conf <<EOF
bind 0.0.0.0
protected-mode yes
port 6379
daemonize no
logfile ""
dir /data
appendonly yes
appendfsync everysec
save ""
maxmemory ${REDIS_MAXMEMORY:-256mb}
maxmemory-policy noeviction
requirepass $REDISCLI_AUTH
EOF

exec redis-server /tmp/y-point-redis.conf
