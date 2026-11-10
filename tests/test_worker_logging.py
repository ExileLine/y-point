# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 02:27:05
# @Author  : yangyuexiong
# @File    : test_worker_logging.py

"""验证 worker 输出在真实描述符重定向后按容量保留最新日志。"""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("preexisting", [False, True])
def test_worker_output_keeps_latest_logs_within_limit(tmp_path: Path, preexisting: bool) -> None:
    """持续输出或已有超限日志时都清理头部，并保留标准流及子进程的最新输出。"""
    log_path = tmp_path / "logs" / "worker.log"
    limit = 16 * 1024
    if preexisting:
        log_path.parent.mkdir()
        log_path.write_bytes(b"old-start\n" + b"old-entry\n" * limit)
    environment = os.environ.copy()
    environment.update(
        {
            "WORKER_LOG_FILE": str(log_path),
            "WORKER_LOG_MAX_BYTES": str(limit),
        },
    )
    script = """
import os
import subprocess
import sys
from app.cli.run_worker import _redirect_output_to_log

_redirect_output_to_log()
print('old-start', flush=True)
for index in range(500):
    print(f'{index:04d} ' + '测试日志' * 16, flush=True)
os.write(1, b'latest-stdout\\n')
os.write(2, b'latest-stderr\\n')
subprocess.run([sys.executable, '-c', "print('latest-child', flush=True)"], check=True)
print('latest-end', flush=True)
"""
    completed = subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, timeout=30)
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    content = log_path.read_bytes()
    assert 0 < len(content) <= limit
    assert b"old-start" not in content
    assert "测试日志" in content.decode("utf-8")
    for marker in (b"latest-stdout\n", b"latest-stderr\n", b"latest-child\n", b"latest-end\n"):
        assert marker in content
    assert list(log_path.parent.iterdir()) == [log_path]


@pytest.mark.parametrize("stop_signal", [signal.SIGINT, signal.SIGTERM])
def test_worker_logging_drains_on_shutdown_signal(tmp_path: Path, stop_signal: int) -> None:
    """开发启动器发送退出信号时，异常展开与退出前的日志也应完整落盘。"""
    log_path = tmp_path / "worker.log"
    environment = os.environ.copy()
    environment["WORKER_LOG_FILE"] = str(log_path)
    script = """
import os
import sys
from app.cli.run_worker import _redirect_output_to_log

_redirect_output_to_log()
try:
    os.kill(os.getpid(), int(sys.argv[1]))
finally:
    print('latest-shutdown', flush=True)
"""
    completed = subprocess.run([sys.executable, "-c", script, str(stop_signal)], env=environment, capture_output=True, timeout=30)
    assert completed.returncode != 0
    assert b"latest-shutdown\n" in log_path.read_bytes()


def test_existing_oversized_worker_log_is_trimmed_at_startup(tmp_path: Path) -> None:
    """即使启动后没有新日志，也要立即收缩已有超限文件并保留原文件标识。"""
    log_path = tmp_path / "worker.log"
    log_path.write_bytes(b"old-start\n" + b"old-entry\n" * 4096 + b"latest-existing\n")
    inode = log_path.stat().st_ino
    environment = os.environ.copy()
    environment.update(
        {
            "WORKER_LOG_FILE": str(log_path),
            "WORKER_LOG_MAX_BYTES": "4096",
        },
    )
    subprocess.run(
        [sys.executable, "-c", "from app.cli.run_worker import _redirect_output_to_log; _redirect_output_to_log()"],
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
    )
    assert log_path.stat().st_ino == inode
    assert log_path.stat().st_size <= 4096
    content = log_path.read_bytes()
    assert b"old-start" not in content
    assert content.endswith(b"latest-existing\n")


def test_default_worker_log_limit_handles_a_long_unicode_line(tmp_path: Path) -> None:
    """实际输出超过 50 MiB 的无换行中文内容，验证默认容量及末尾日志保留。"""
    log_path = tmp_path / "worker.log"
    environment = os.environ.copy()
    environment["WORKER_LOG_FILE"] = str(log_path)
    environment.pop("WORKER_LOG_MAX_BYTES", None)
    script = """
import os
from app.cli.run_worker import _redirect_output_to_log

_redirect_output_to_log()
content = ('长日志' * 4096).encode()
for _ in range(1600):
    remaining = memoryview(content)
    while remaining:
        remaining = remaining[os.write(1, remaining):]
os.write(2, b'\\nlatest-long-line\\n')
"""
    subprocess.run([sys.executable, "-c", script], env=environment, capture_output=True, check=True, timeout=30)
    assert 45 * 1024 * 1024 <= log_path.stat().st_size <= 50 * 1024 * 1024
    with log_path.open("rb") as stream:
        assert stream.read(30).decode("utf-8")
        stream.seek(-64, os.SEEK_END)
        assert b"latest-long-line" in stream.read()
