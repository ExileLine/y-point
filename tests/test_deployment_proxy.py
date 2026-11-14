# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:57:57
# @Author  : yangyuexiong
# @File    : test_deployment_proxy.py

"""用隔离 Docker 网络验证最终 Web 镜像的真实 TLS、SSE 与请求标识传递。"""

import os
import shutil
import ssl
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest


@pytest.mark.skipif(os.getenv("YPOINT_TEST_DEPLOYMENT_PROXY") != "1", reason="需显式启用并先构建本机部署 Web 镜像")
@pytest.mark.parametrize("https", [False, True])
def test_real_nginx_streams_sse_and_overwrites_untrusted_request_id(tmp_path: Path, https: bool) -> None:
    """HTTP 无证书与 HTTPS 均及时转发事件及 Cookie，并覆盖外来请求 ID。"""
    tls = tmp_path.resolve()
    if https:
        if shutil.which("openssl") is None:
            pytest.skip("HTTPS 验证需要 OpenSSL")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=IP:127.0.0.1",
                "-keyout",
                str(tls / "tls.key"),
                "-out",
                str(tls / "tls.crt"),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
    suffix = uuid4().hex[:12]
    network = f"yp-deploy-proxy-{suffix}"
    upstream = f"yp-deploy-upstream-{suffix}"
    web = f"yp-deploy-web-{suffix}"

    def docker(*arguments: str) -> str:
        """仅操作本用例随机命名的 Docker 资源，不访问业务部署的数据或容器。"""
        return subprocess.check_output(["docker", *arguments], text=True, stderr=subprocess.STDOUT, timeout=45)

    source = """
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    '提供延迟发送的两个 SSE 事件。'
    def do_GET(self):
        '返回公开探针事件与实际收到的代理头。'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('X-Probe-Request-ID', self.headers.get('X-Request-ID', 'missing'))
        self.send_header('X-Probe-Cookie', str(self.headers.get('Cookie') == 'deployment_probe=ok'))
        self.end_headers()
        self.wfile.write(b'data: first\\n\\n')
        self.wfile.flush()
        time.sleep(1.25)
        self.wfile.write(b'data: second\\n\\n')
        self.wfile.flush()
HTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
"""
    try:
        docker("network", "create", network)
        docker(
            "run",
            "-d",
            "--pull=never",
            "--rm",
            "--name",
            upstream,
            "--network",
            network,
            "--network-alias",
            "api",
            "--log-driver=local",
            "python:3.12-slim-bookworm",
            "python",
            "-u",
            "-c",
            source,
        )
        # Nginx 的健康入口不依赖上游；先确认探针服务监听，消除测试启动竞态。
        docker(
            "exec",
            upstream,
            "python",
            "-c",
            """
import socket, time
until = time.monotonic() + 15
while True:
    try:
        with socket.create_connection(('127.0.0.1', 8000), timeout=1):
            break
    except OSError:
        if time.monotonic() >= until:
            raise
        time.sleep(0.1)
""",
        )
        docker(
            "run",
            "-d",
            "--pull=never",
            "--rm",
            "--name",
            web,
            "--network",
            network,
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
            "--env",
            f"YPOINT_HTTPS_ENABLED={str(https).lower()}",
            *(["--mount", f"type=bind,src={tls},dst=/run/tls,readonly"] if https else []),
            "--publish",
            f"127.0.0.1::{8443 if https else 8080}",
            "--log-driver=local",
            "y-point-web:local",
        )
        port = docker("port", web, f"{8443 if https else 8080}/tcp").strip().rsplit(":", 1)[1]
        context = ssl.create_default_context(cafile=str(tls / "tls.crt")) if https else None
        base_url = f"{'https' if https else 'http'}://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            try:
                with urlopen(f"{base_url}/healthz", context=context, timeout=2) as response:
                    assert response.status == 200
                break
            except (URLError, OSError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        with urlopen(f"{base_url}/agents/1/sessions", context=context, timeout=5) as response:
            assert response.status == 200
            assert b'<div id="root">' in response.read()
        request = Request(
            f"{base_url}/api/v1/runs/proxy-probe/stream",
            headers={
                "X-Request-ID": "untrusted-client-id",
                "Cookie": "deployment_probe=ok",
            },
        )
        with urlopen(request, context=context, timeout=5) as response:
            assert response.headers["Content-Type"].startswith("text/event-stream")
            identifier = response.headers["X-Request-ID"]
            assert identifier != "untrusted-client-id"
            assert response.headers["X-Probe-Request-ID"] == identifier
            assert response.headers["X-Probe-Cookie"] == "True"
            assert response.readline() == b"data: first\n"
            first_at = time.monotonic()
            assert response.readline() == b"\n"
            assert response.readline() == b"data: second\n"
            assert time.monotonic() - first_at >= 0.9
    except Exception:
        # 仅输出隔离探针的日志，便于定位上游启动或代理配置故障。
        for container in (web, upstream):
            result = subprocess.run(["docker", "logs", "--tail", "20", container], capture_output=True, text=True, timeout=10)
            print(result.stdout, result.stderr)
        raise
    finally:
        subprocess.run(["docker", "rm", "-f", web, upstream], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
