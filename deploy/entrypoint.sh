#!/bin/sh
set -eu
umask 077
python /app/deploy/preflight.py
exec "$@"

# -e  对当前脚本中的普通顺序命令，执行失败就退出
# -u  使用未定义的变量时报错并退出
# umask 077：限制新建文件的默认权限
# exec "$@"：执行传进来的服务命令
# "$@"：取出传给脚本的所有参数，并保留每个参数的边界