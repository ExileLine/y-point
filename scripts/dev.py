# -*- coding: utf-8 -*-
# @Time    : 2026-10-30 19:17:36
# @Author  : yangyuexiong
# @File    : dev.py

"""同时运行 API、执行器和前端开发服务。"""

import os
import fcntl
import json
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
API_PORT = 7769
WEB_PORT = 5173
LOCK_PATH = ROOT / ".run-private" / "dev.lock"


def process_start_time(pid: int) -> str | None:
    """读取进程启动时间，避免过期 PID 被系统重用后误停其他进程。"""
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


@contextmanager
def dev_lock(path: Path, timeout_seconds: float = 20) -> Iterator[None]:
    """串行化本项目开发服务启动，并等待旧管理进程清理子进程。"""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + timeout_seconds
        signaled: set[tuple[int, str]] = set()
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                lock_file.seek(0)
                try:
                    owner = json.load(lock_file)
                    pid = owner["pid"]
                    started = owner["started"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise RuntimeError("开发服务锁由未知进程占用，无法安全停止") from exc
                if not isinstance(pid, int) or pid <= 0 or not isinstance(started, str) or process_start_time(pid) != started:
                    raise RuntimeError("开发服务锁进程身份不匹配，无法安全停止")
                identity = (pid, started)
                if identity not in signaled:
                    print(f"dev: stopping existing services (pid {pid})", flush=True)
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    signaled.add(identity)
                if time.monotonic() >= deadline:
                    raise RuntimeError("旧开发服务未在规定时间内退出")
                time.sleep(0.1)
        try:
            started = process_start_time(os.getpid())
            if not started:
                raise RuntimeError("无法确认当前开发进程身份")
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(
                {
                    "pid": os.getpid(),
                    "started": started,
                },
                lock_file,
            )
            lock_file.flush()
            yield
        finally:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
            fcntl.flock(lock_file, fcntl.LOCK_UN)


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


def stop_processes(processes: list[subprocess.Popen], grace_seconds: float = 5) -> None:
    """终止本次启动的进程组并等待退出。"""
    groups = {process.pid for process in processes}
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + grace_seconds
    while groups and time.monotonic() < deadline:
        for process in processes:
            process.poll()
        remaining = set()
        for group in groups:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                continue
            remaining.add(group)
        groups = remaining
        if groups:
            time.sleep(0.05)

    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    for process in processes:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run_services() -> int:
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


def main() -> int:
    """先接管本项目已有开发服务，再启动新一组服务。"""
    try:
        with dev_lock(LOCK_PATH):
            return run_services()
    except RuntimeError as exc:
        print(f"dev: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
