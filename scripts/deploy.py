# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:17:57
# @Author  : yangyuexiong
# @File    : deploy.py

"""生成隔离部署的私有配置，并统一传入 Compose 环境文件与覆盖配置。"""

import argparse
import base64
from ipaddress import IPv4Address
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def private_file(path: Path, content: str, uid: int, gid: int) -> None:
    """以最小文件权限创建私有配置，root 初始化时将其交给运行用户。"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    if os.getuid() == 0:
        os.chown(path, uid, gid)


def read_private_values(path: Path) -> dict[str, str]:
    """读取部署脚本生成的环境文件，支持引号和行尾注释，不执行变量或命令。"""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = " ".join(shlex.split(value, comments=True))
    return values


def configured_https(values: dict[str, str]) -> bool:
    """读取持久化协议开关；旧配置未记录开关时保留原来的 HTTPS 行为。"""
    value = values.get("YPOINT_HTTPS_ENABLED", "true").lower()
    if value not in {"true", "false"}:
        raise SystemExit("YPOINT_HTTPS_ENABLED 仅支持 true 或 false")
    return value == "true"


def update_private_values(path: Path, updates: dict[str, str]) -> None:
    """原子更新指定配置项，保留其他配置、文件所属用户和私有权限。"""
    original = path.read_text(encoding="utf-8")
    remaining = dict(updates)
    lines = []
    for line in original.splitlines():
        key = line.partition("=")[0].strip()
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            remaining.pop(key, None)
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in remaining.items())
    content = "\n".join(lines) + "\n"
    if content == original:
        return
    metadata = path.stat()
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            if os.getuid() == 0:
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            stream.write(content)
            stream.flush()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def initialize(args: argparse.Namespace, directory: Path) -> None:
    """生成部署配置；显式切换 HTTP/HTTPS 时仅更新协议和 Cookie，不轮换业务凭据。"""
    if any(char in str(directory) for char in ("\n", "\r", '"', "'", "$")):
        raise SystemExit("部署目录不能包含换行、引号或 $ 字符")
    data = directory / "data"
    tls = directory / "tls"
    files = [directory / name for name in ("compose.env", "application.env", "database.env")]
    existing = [path.exists() for path in files]
    if any(existing) and not all(existing):
        raise SystemExit("部署配置不完整：请从备份恢复同一套配置，不能自动生成新数据库密码或加密密钥")
    previous = read_private_values(files[0]) if all(existing) else {}
    if previous and previous.get("COMPOSE_PROJECT_NAME") != f"y-point-{args.environment}":
        raise SystemExit("配置目录属于其他部署环境，请使用独立目录或匹配的 --environment")
    certificates = args.tls_cert is not None or args.tls_key is not None
    if certificates and not (args.tls_cert and args.tls_key):
        raise SystemExit("--tls-cert 和 --tls-key 必须同时提供")
    if certificates and args.https is False:
        raise SystemExit("--http 不能同时指定 TLS 证书")
    if certificates and not all(path.is_file() for path in (args.tls_cert, args.tls_key)):
        raise SystemExit("TLS 证书或私钥文件不存在")
    https = configured_https(previous) if previous else False
    if certificates:
        https = True
    if args.https is not None:
        https = args.https
    http_port = args.http_port if args.http_port is not None else int(previous.get("YPOINT_HTTP_PORT", "18080"))
    https_port = args.https_port if args.https_port is not None else int(previous.get("YPOINT_HTTPS_PORT", "18443"))
    if not 1 <= http_port <= 65535 or (https and (not 1 <= https_port <= 65535 or http_port == https_port)):
        raise SystemExit("HTTP 端口需在 1-65535 内；启用 HTTPS 时两个端口均需有效且不同")
    has_certificate = (tls / "tls.crt").is_file() and (tls / "tls.key").is_file()
    if https and not certificates and not has_certificate:
        if previous and configured_https(previous):
            raise SystemExit("部署 TLS 证书缺失，请恢复证书、提供新证书或使用 init --http 切换协议")
        if args.environment != "local":
            raise SystemExit("staging/production 启用 HTTPS 时必须传入 --tls-cert 和 --tls-key 正式证书路径")
    uid = args.uid if args.uid is not None else int(previous.get("YPOINT_UID", os.getuid() or 10001))
    gid = args.gid if args.gid is not None else int(previous.get("YPOINT_GID", os.getgid() if os.getuid() else 10001))
    directories = [directory, data, data / "runtime", data / "artifacts", data / "tool-environments", data / "tool-sandbox"]
    if https:
        directories.append(tls)
    for path in directories:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.getuid() == 0:
            os.chown(path, uid, gid)
    if https:
        if certificates:
            for source, target in ((args.tls_cert, tls / "tls.crt"), (args.tls_key, tls / "tls.key")):
                if source.resolve() != target.resolve():
                    shutil.copyfile(source, target)
        elif not has_certificate:
            subprocess.run(
                [
                    "openssl",
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-sha256",
                    "-nodes",
                    "-days",
                    "30",
                    "-subj",
                    "/CN=localhost",
                    "-addext",
                    "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
                    "-keyout",
                    str(tls / "tls.key"),
                    "-out",
                    str(tls / "tls.crt"),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
        for path in (tls / "tls.crt", tls / "tls.key"):
            path.chmod(0o600)
            if os.getuid() == 0:
                os.chown(path, uid, gid)
    if all(existing):
        compose_updates = {
            "YPOINT_HTTPS_ENABLED": f"'{str(https).lower()}'",
        }
        for key, value in (("YPOINT_BIND_IP", args.bind_ip), ("YPOINT_HTTP_PORT", args.http_port), ("YPOINT_HTTPS_PORT", args.https_port)):
            if value is not None:
                compose_updates[key] = f"'{value}'"
        update_private_values(files[0], compose_updates)
        if args.https is not None or certificates or "STREAM_COOKIE_SECURE" not in read_private_values(files[1]):
            update_private_values(files[1], {"STREAM_COOKIE_SECURE": str(https)})
        print("保留现有数据库密码、JWT 密钥和供应商加密密钥；同步协议与 SSE Cookie 配置。")
    else:
        password = secrets.token_urlsafe(36)
        values = {
            "ENV": "staging" if args.environment == "staging" else "production",
            "SECRET_KEY": secrets.token_urlsafe(48),
            "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "DB_PASSWORD": password,
            "DATA_DIR": str(data),
            "STREAM_COOKIE_SECURE": str(https),
        }
        template = (ROOT / "deploy/config/application.env.example").read_text(encoding="utf-8")
        for key, value in values.items():
            template = template.replace("{{" + key + "}}", value)
        socket = Path(args.docker_socket)
        compose = {
            "COMPOSE_PROJECT_NAME": f"y-point-{args.environment}",
            "YPOINT_ENV": values["ENV"],
            "YPOINT_UID": str(uid),
            "YPOINT_GID": str(gid),
            "YPOINT_APP_ENV": str(directory / "application.env"),
            "YPOINT_DB_ENV": str(directory / "database.env"),
            "YPOINT_DATA_DIR": str(data),
            "YPOINT_TLS_DIR": str(tls),
            "YPOINT_BIND_IP": str(args.bind_ip or "127.0.0.1"),
            "YPOINT_HTTP_PORT": str(http_port),
            "YPOINT_HTTPS_PORT": str(https_port),
            "YPOINT_HTTPS_ENABLED": str(https).lower(),
            "YPOINT_SERVER_IMAGE": f"y-point-server:{args.environment}",
            "YPOINT_WEB_IMAGE": f"y-point-web:{args.environment}",
            "YPOINT_DOCKER_SOCKET": str(socket),
            "YPOINT_DOCKER_GID": str(args.docker_gid if args.docker_gid is not None else (socket.stat().st_gid if socket.exists() else 0)),
            "YPOINT_LOG_MAX_SIZE": "20m",
            "YPOINT_LOG_MAX_FILES": "5",
        }
        private_file(directory / "application.env", template, uid, gid)
        private_file(directory / "database.env", f"POSTGRES_USER=y_point\nPOSTGRES_DB=y_point\nPOSTGRES_PASSWORD={password}\n", uid, gid)
        private_file(directory / "compose.env", "".join(f"{key}='{value}'\n" for key, value in compose.items()), uid, gid)
    # 两个服务通过同一路径向引擎提交受信任的代理脚本；原子替换避免读到半份文件。
    proxy = data / "runtime/package_proxy.py"
    temporary = proxy.with_suffix(".tmp")
    shutil.copyfile(ROOT / "sandbox/package_proxy.py", temporary)
    temporary.chmod(0o644)
    if os.getuid() == 0:
        os.chown(temporary, uid, gid)
    temporary.replace(proxy)
    print(f"部署配置：{directory / 'compose.env'}")
    print(f"私有数据：{data}")
    print(f"访问协议：{'HTTPS（HTTP 自动跳转）' if https else 'HTTP'}")
    if https and args.environment == "local":
        print("local 的自签证书仅供本机调试，有效期 30 天；浏览器需信任证书或确认例外。")


def main() -> None:
    """选择部署环境并执行初始化或原样透传 Compose 子命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "compose"))
    parser.add_argument("--environment", choices=("local", "staging", "production"), default="local")
    parser.add_argument("--directory", type=Path, help="私有部署目录，默认 .deployment/<environment>")
    parser.add_argument("--sandbox", action="store_true", help="显式允许 API/worker 管理本机 Docker")
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--docker-socket", default="/var/run/docker.sock")
    parser.add_argument("--docker-gid", type=int)
    parser.add_argument("--bind-ip", type=IPv4Address, help="init 时设置宿主 IPv4 监听地址，新配置默认 127.0.0.1")
    parser.add_argument("--http-port", type=int, help="init 时设置 HTTP 端口，新配置默认 18080")
    parser.add_argument("--https-port", type=int, help="init 时设置 HTTPS 端口，新配置默认 18443")
    transport = parser.add_mutually_exclusive_group()
    transport.add_argument("--https", dest="https", action="store_true", help="init 时启用 HTTPS；local 可自动生成自签证书")
    transport.add_argument("--http", dest="https", action="store_false", help="init 时使用 HTTP，不要求证书")
    parser.set_defaults(https=None)
    parser.add_argument("--tls-cert", type=Path)
    parser.add_argument("--tls-key", type=Path)
    args, forwarded = parser.parse_known_args()
    directory = (args.directory or ROOT / ".deployment" / args.environment).expanduser().resolve()
    if args.command == "init":
        if forwarded:
            parser.error("init 不接受额外参数")
        initialize(args, directory)
        return
    if any(value is not None for value in (args.https, args.tls_cert, args.tls_key, args.bind_ip, args.http_port, args.https_port)):
        parser.error("协议、证书及监听选项仅用于 init；compose 自动读取已保存的配置")
    config = directory / "compose.env"
    if not config.is_file():
        parser.error("请先执行 init 生成本环境的私有部署配置")
    command = ["docker", "compose", "--env-file", str(config), "-f", str(ROOT / "docker-compose.yml")]
    if args.environment == "production":
        command.extend(["-f", str(ROOT / "docker-compose.production.yml")])
    if configured_https(read_private_values(config)):
        command.extend(["-f", str(ROOT / "docker-compose.https.yml")])
    if args.sandbox:
        command.extend(["-f", str(ROOT / "docker-compose.sandbox.yml")])
    if forwarded[:1] == ["--"]:
        forwarded = forwarded[1:]
    if not forwarded:
        forwarded = ["ps"]
    raise SystemExit(subprocess.call([*command, *forwarded], cwd=ROOT))


if __name__ == "__main__":
    main()
