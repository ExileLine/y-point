# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 11:30:00
# @Author  : yangyuexiong
# @File    : test_dev_launcher.py

"""验证开发服务管理锁的重复启动与进程身份保护。"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from scripts.dev import dev_lock, stop_processes


def test_second_dev_launch_stops_first(tmp_path: Path) -> None:
    """第二次启动先终止同项目旧管理进程，再获取开发锁。"""
    lock_path = tmp_path / "dev.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal, sys, time; from pathlib import Path; from scripts.dev import dev_lock; "
                "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
                "lock = dev_lock(Path(sys.argv[1])); lock.__enter__(); print('ready', flush=True); "
                "time.sleep(30)"
            ),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with dev_lock(lock_path, timeout_seconds=5):
            assert child.wait(timeout=2) == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


def test_stop_processes_cleans_child_after_uv_wrapper_exits() -> None:
    """包装进程先退出时，仍须清理同组中忽略 SIGTERM 的子进程。"""
    child_script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
    wrapper_script = (
        "import subprocess, sys; "
        "child = subprocess.Popen([sys.executable, '-c', sys.argv[1]], stdout=subprocess.PIPE, text=True); "
        "assert child.stdout.readline().strip() == 'ready'; "
        "print(child.pid, flush=True)"
    )
    wrapper = subprocess.Popen(
        [sys.executable, "-c", wrapper_script, child_script],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    assert wrapper.stdout is not None
    child_pid = int(wrapper.stdout.readline().strip())
    try:
        assert wrapper.wait(timeout=2) == 0
        assert os.getpgid(child_pid) == wrapper.pid
        stop_processes([wrapper], grace_seconds=0.2)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = subprocess.run(
                ["ps", "-p", str(child_pid), "-o", "stat="],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            if not state or state.startswith("Z"):
                break
            time.sleep(0.05)
        assert not state or state.startswith("Z")
    finally:
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wrapper.wait(timeout=2)
