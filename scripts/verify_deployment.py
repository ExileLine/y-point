# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:39:17
# @Author  : yangyuexiong
# @File    : verify_deployment.py

"""验证已启动的隔离 Docker 部署，不创建账号、不提交模型任务、不修改业务数据。"""

import argparse
import json
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class NoRedirect(HTTPRedirectHandler):
    """保留 HTTP 重定向响应供验证，不跳到系统尚未信任的本机证书。"""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        """阻止 urllib 自动跟随跳转。"""
        return None


def main() -> None:
    """按部署协议核对页面、API、worker、持久化挂载和可选的真实 Docker 沙箱。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=("local", "staging", "production"), default="local")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument("--restart", action="store_true", help="重新创建 API 并验证代理重新发现；仅在无活跃任务的隔离部署执行")
    args = parser.parse_args()
    directory = (args.directory or ROOT / ".deployment" / args.environment).resolve()
    # 与部署入口使用同一套配置解析，兼容旧版只支持 HTTPS 的部署文件。
    from scripts.deploy import configured_https, read_private_values

    values = read_private_values(directory / "compose.env")
    https_enabled = configured_https(values)
    context = ssl.create_default_context(cafile=str(directory / "tls/tls.crt")) if https_enabled else None
    https = f"https://localhost:{values['YPOINT_HTTPS_PORT']}"
    http = f"http://localhost:{values['YPOINT_HTTP_PORT']}"
    base_url = https if https_enabled else http
    command = ["python3", str(ROOT / "scripts/deploy.py"), "compose", "--environment", args.environment, "--directory", str(directory)]
    if args.sandbox:
        command.append("--sandbox")

    def compose(*arguments: str) -> str:
        """执行选定部署的只读检查及显式请求的重建操作，避免输出私有配置。"""
        result = subprocess.run([*command, "--", *arguments], check=False, capture_output=True, text=True, timeout=180)
        if result.returncode:
            raise RuntimeError(f"Compose {arguments[0]} 失败：{result.stderr[-2000:]}")
        return result.stdout

    def check(path: str, status: int = 200) -> tuple[bytes, object]:
        """核对所选协议的响应；HTTPS 模式使用本环境证书，不关闭证书校验。"""
        try:
            response = urlopen(Request(base_url + path), context=context, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            assert response.status == status, f"HTTP {response.status}: {path}"
            return response.read(), response.headers

    if https_enabled:
        try:
            build_opener(NoRedirect).open(http + "/agents", timeout=10)
            raise AssertionError("HTTP 未重定向到 HTTPS")
        except HTTPError as error:
            assert error.code == 308
            assert error.headers["Location"] == https + "/agents"
    for path in ("/", "/agents", "/agents/1/sessions", "/knowledge-bases"):
        body, headers = check(path)
        assert b'<div id="root">' in body and "no-cache" in headers["Cache-Control"]
    body, headers = check("/api/v1/health")
    assert json.loads(body)["data"]["status"] == "ok"
    request_id = headers["X-Request-ID"]
    assert request_id and headers["X-Content-Type-Options"] == "nosniff"
    body, _ = check("/api/v1/health/worker")
    assert json.loads(body)["data"]["worker_available"] is True
    body, _ = check("/api/v1/not-a-real-route", 404)
    assert b'<div id="root">' not in body
    check("/assets/not-a-real-asset.js", 404)
    print(f"{'HTTPS 与 HTTP 跳转' if https_enabled else 'HTTP（无证书）'}、SPA、API、worker 和错误路由通过。")
    for service in ("api", "worker", "web", "db"):
        identifier = compose("ps", "-q", service).strip()
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{json .HostConfig.LogConfig}}", identifier],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        logging = json.loads(result.stdout)
        assert logging["Type"] == "local"
        assert logging["Config"]["max-size"] == values["YPOINT_LOG_MAX_SIZE"]
        assert logging["Config"]["max-file"] == values["YPOINT_LOG_MAX_FILES"]
    compose("exec", "-T", "api", "alembic", "check")
    compose("exec", "-T", "worker", "python", "/app/deploy/healthcheck.py", "worker")
    compose("exec", "-T", "worker", "python", "-c", "from pathlib import Path; assert not Path('/app/.logs/worker.log').exists()")
    logs = compose("logs", "--no-color", "--since", "5m", "api", "web")
    assert request_id in logs
    runtime = """
import subprocess
import sys
import pdfplumber
import pypdfium2
from PIL import Image
languages = subprocess.check_output(['tesseract', '--list-langs'], text=True)
assert {'chi_sim', 'eng'}.issubset(languages.splitlines())
font = subprocess.check_output(['fc-match', 'sans:lang=zh-cn'], text=True)
assert 'Noto' in font
"""
    compose("exec", "-T", "worker", "python", "-c", runtime)
    print("迁移结构、专属心跳、镜像 PDF/OCR 依赖、日志轮转与请求 ID 追踪通过。")
    if args.sandbox:
        script = """
import asyncio, json
from app.core.config import get_config
from app.services.script_sandbox import run_script
from app.services.tool_environments import _package_network
async def exercise():
    source = 'import json,sys; value=json.load(sys.stdin); print(json.dumps({"answer":value["value"]+1}))'
    result = await run_script('python', source, {'value': 41}, 10)
    assert json.loads(result) == {'answer': 42}
    async with _package_network(get_config().SCRIPT_SANDBOX_IMAGE) as network:
        assert network.startswith('yp-deps-net-')
asyncio.run(exercise())
"""
        for service in ("api", "worker"):
            compose("exec", "-T", service, "python", "-c", script)
        print("API 与 worker 的真实 Docker 脚本、同路径挂载和受信任代理启动/清理通过。")
    if args.restart:
        marker = directory / "data/runtime/.deployment-probe"
        marker.write_text("persistent", encoding="utf-8")
        compose("up", "-d", "--no-deps", "--force-recreate", "api")
        deadline = time.monotonic() + 90
        while True:
            try:
                body, _ = check("/api/v1/health")
                assert json.loads(body)["data"]["status"] == "ok"
                break
            except (AssertionError, URLError, OSError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        compose("exec", "-T", "api", "python", "-c", f"from pathlib import Path; assert Path({str(marker)!r}).read_text() == 'persistent'")
        marker.unlink()
        print("API 容器重新创建后，Nginx 重新发现和持久化文件访问通过。")
    print("部署检查通过；本检查不证明真实模型、业务审查质量、完整 SSE 会话或目标环境容量。")


if __name__ == "__main__":
    main()
