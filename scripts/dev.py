# -*- coding: utf-8 -*-
# @Time    : 2026-10-30 19:17:36
# @Author  : yangyuexiong
# @File    : dev.py

"""同时运行 API、执行器和前端开发服务。"""

import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_PORT = 7769
WEB_PORT = 5173


def port_available(port: int) -> bool:
    """检查本地端口是否可供开发服务监听。"""
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def check_prerequisites() -> int:
    """检查依赖和端口，并返回可用的前端端口。"""
    for command in ("uv", "npm"):
        if shutil.which(command) is None:
            raise RuntimeError(f"Missing {command} in PATH")

    if not (ROOT / "web/node_modules/.bin/vite").exists():
        raise RuntimeError("Missing web dependencies; run npm install in web/")

    if not port_available(API_PORT):
        raise RuntimeError(f"API port {API_PORT} is already in use; stop the existing service first")

    for port in range(WEB_PORT, WEB_PORT + 20):
        if port_available(port):
            return port
    raise RuntimeError(f"No available web port from {WEB_PORT} to {WEB_PORT + 19}")


def stop_processes(processes: list[subprocess.Popen]) -> None:
    """终止本次启动的进程组并等待退出。"""
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + 5
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def main() -> int:
    """启动并监控三个开发服务，退出时清理子进程。"""
    try:
        web_port = check_prerequisites()
    except RuntimeError as exc:
        print(f"dev: {exc}", file=sys.stderr)
        return 1

    web_env = os.environ.copy()
    web_env["API_PROXY_TARGET"] = f"http://127.0.0.1:{API_PORT}"
    services = (
        ("api", ["uv", "run", "python", "local_run.py"], ROOT, None),
        ("worker", ["uv", "run", "python", "-m", "app.cli.run_worker"], ROOT, None),
        ("web", ["npm", "run", "dev", "--", "--port", str(web_port), "--strictPort"], ROOT / "web", web_env),
    )
    processes: list[subprocess.Popen] = []
    stopped_by_signal = None

    def request_stop(signum: int, _frame: object) -> None:
        """记录终止信号，由主循环统一清理子进程。"""
        nonlocal stopped_by_signal
        stopped_by_signal = signum

    previous_int = signal.signal(signal.SIGINT, request_stop)
    previous_term = signal.signal(signal.SIGTERM, request_stop)
    try:
        for name, command, cwd, env in services:
            if stopped_by_signal is not None:
                break
            process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
            processes.append(process)
            print(f"dev: started {name} (pid {process.pid})", flush=True)

        if stopped_by_signal is None:
            print(f"dev: web http://127.0.0.1:{web_port} | API http://127.0.0.1:{API_PORT}", flush=True)
            print("dev: press Ctrl-C to stop all three services", flush=True)
        while stopped_by_signal is None:
            for name, process in zip((item[0] for item in services), processes):
                return_code = process.poll()
                if return_code is not None:
                    print(f"dev: {name} exited ({return_code}); stopping remaining services", file=sys.stderr)
                    return 1
            time.sleep(0.2)
        return 0 if stopped_by_signal == signal.SIGINT else 128 + stopped_by_signal
    except OSError as exc:
        print(f"dev: failed to start service: {exc}", file=sys.stderr)
        return 1
    finally:
        stop_processes(processes)
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    sys.exit(main())
