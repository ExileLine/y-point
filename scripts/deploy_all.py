# -*- coding: utf-8 -*-
# @Time    : 2026-11-14 17:44:24
# @Author  : yangyuexiong
# @File    : deploy_all.py

"""一键完成 Docker 部署检查、初始化、构建和健康等待，复用 deploy.py 的私有配置与环境选择。"""

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from ipaddress import IPv4Address
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, capture: bool = False) -> str:
    """打印可复现命令并执行；配置 JSON 仅在内存读取，不把私有值输出到终端。"""
    print(f"$ {shlex.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=capture)
    return completed.stdout if capture else ""


def positive_integer(value: str) -> int:
    """拒绝非正数等待时限，避免在初始化后才发现部署参数错误。"""
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def main(argv: list[str] | None = None) -> None:
    """串行部署选定环境，失败即停并保留现场；管理员密码只通过交互终端输入。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("local", "staging", "production"), default="local")
    parser.add_argument("--directory", type=Path, help="私有部署目录，默认项目内 .deployment/<environment>")
    parser.add_argument("--bind-ip", type=IPv4Address, help="宿主 IPv4 地址；新配置默认 127.0.0.1，服务器可指定内网 IP")
    parser.add_argument("--http-port", type=int, help="新配置默认 18080；显式传入会更新已保存的端口")
    parser.add_argument("--https-port", type=int, help="新配置默认 18443；显式传入会更新已保存的端口")
    protocol = parser.add_mutually_exclusive_group()
    protocol.add_argument("--http", dest="https", action="store_false", help="显式使用 HTTP，无需证书")
    protocol.add_argument("--https", dest="https", action="store_true", help="显式启用 HTTPS，local 可生成自签证书")
    parser.set_defaults(https=None)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    parser.add_argument("--sandbox", action="store_true", help="显式启用受信任本机 Docker 脚本沙箱，并构建沙箱镜像")
    parser.add_argument("--skip-build", action="store_true", help="复用已有镜像；跳过应用和沙箱镜像构建")
    parser.add_argument("--wait-timeout", type=positive_integer, default=180, help="健康等待秒数，默认 180")
    parser.add_argument("--create-admin", metavar="USERNAME", help="服务就绪后交互创建首位管理员；仅首次部署需要")
    args = parser.parse_args(argv)
    if args.create_admin and not sys.stdin.isatty():
        parser.error("--create-admin 需要交互终端输入密码；自动化部署请省略，启动后单独创建首位管理员")
    if sys.version_info < (3, 12):
        parser.error("部署入口需要 Python 3.12 或以上版本")
    if shutil.which("docker") is None:
        parser.error("未找到 docker，请先安装 Docker Engine/Desktop 与 Compose 插件")

    directory = (args.directory or ROOT / ".deployment" / args.environment).expanduser().resolve()
    common = ["--environment", args.environment, "--directory", str(directory)]
    initialize = [sys.executable, str(ROOT / "scripts/deploy.py"), "init", *common]
    if args.https is not None:
        initialize.append("--https" if args.https else "--http")
    for flag, value in (
        ("--bind-ip", args.bind_ip),
        ("--http-port", args.http_port),
        ("--https-port", args.https_port),
        ("--tls-cert", args.tls_cert.expanduser().resolve() if args.tls_cert else None),
        ("--tls-key", args.tls_key.expanduser().resolve() if args.tls_key else None),
    ):
        if value is not None:
            initialize.extend([flag, str(value)])
    compose = [sys.executable, str(ROOT / "scripts/deploy.py"), "compose", *common]
    if args.sandbox:
        compose.append("--sandbox")
    compose.append("--")

    try:
        print("[1/5] 检查 Docker 与 Compose", flush=True)
        run(["docker", "compose", "version", "--short"], capture=True)
        run(["docker", "info", "--format", "{{.ServerVersion}}"], capture=True)
        print("[2/5] 初始化或复用私有部署配置", flush=True)
        run(initialize)
        # 配置输出含密码，必须 capture，不能直接打印或写到日志文件。
        config = json.loads(run([*compose, "config", "--format", "json"], capture=True))
        print("[3/5] 准备运行镜像", flush=True)
        if args.sandbox:
            image = config["services"]["api"]["environment"]["SCRIPT_SANDBOX_IMAGE"]
            if args.skip_build:
                run(["docker", "image", "inspect", image], capture=True)
            else:
                run(["docker", "build", "-f", str(ROOT / "Dockerfile.sandbox"), "-t", image, str(ROOT)])
        if not args.skip_build:
            run([*compose, "build"])
        print("[4/5] 启动数据库、迁移、API、worker 和 Web，等待健康检查", flush=True)
        run([*compose, "up", "-d", "--no-build", "--wait", "--wait-timeout", str(args.wait_timeout)])
        print("[5/5] 查看服务状态", flush=True)
        run([*compose, "ps"])
    except subprocess.CalledProcessError as error:
        print(f"部署中止：命令退出码 {error.returncode}；已保留配置、数据和容器，便于修复后重新执行。", file=sys.stderr)
        print(f"查看日志：{shlex.join([*compose, 'logs', '--tail', '100', 'api', 'worker', 'web', 'db'])}", file=sys.stderr)
        raise SystemExit(error.returncode) from error

    web = config["services"]["web"]
    https = web["environment"].get("YPOINT_HTTPS_ENABLED") == "true"
    target = 8443 if https else 8080
    port = next(port for port in web["ports"] if port["target"] == target)
    host = port.get("host_ip", "0.0.0.0")
    if host in {"0.0.0.0", "::"}:
        host = "<服务器IP>"
    elif ":" in host:
        host = f"[{host}]"
    print(f"\n部署完成。访问：{'https' if https else 'http'}://{host}:{port['published']}")
    print(f"私有配置与数据目录：{directory}")
    print(f"查看日志：{shlex.join([*compose, 'logs', '--follow', '--tail', '100'])}")
    admin = [*compose, "exec", "api", "python", "-m", "app.cli.create_admin", args.create_admin or "admin"]
    if args.create_admin:
        try:
            run(admin)
        except subprocess.CalledProcessError as error:
            print("服务已部署，首位管理员未创建成功；已有管理员时无需再次创建。", file=sys.stderr)
            raise SystemExit(error.returncode) from error
    else:
        print(f"首次使用请交互创建管理员：{shlex.join(admin)}")


if __name__ == "__main__":
    main()
