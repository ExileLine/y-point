# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 02:29:05
# @Author  : yangyuexiong
# @File    : worker_logging.py

"""收集 worker 标准流，并在同一日志文件中循环保留最新内容。"""

import atexit
import fcntl
import os
import sys
import threading
from contextlib import suppress
from pathlib import Path


def _align_tail(content: bytes) -> bytes:
    """优先丢弃截断的首行；单个超长行则避开 UTF-8 延续字节保留尾部。"""
    newline = content.find(b"\n", 0, 4096)
    if 0 <= newline < len(content) - 1:
        return content[newline + 1 :]
    index = 0
    while index < len(content) and 0x80 <= content[index] <= 0xBF:
        index += 1
    return content[index:]


class WorkerLogCapture:
    """通过管道收集描述符输出，并在进程间文件锁保护下清理旧日志。"""

    def __init__(self, path: Path, max_bytes: int) -> None:
        """打开日志并先收缩已有超限文件，再重定向标准输出和标准错误。"""
        if max_bytes <= 0:
            raise ValueError("worker 日志容量必须大于零")
        self.max_bytes = max_bytes
        self._closed = False
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._log_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            self._append(b"")
        except BaseException:
            os.close(self._log_fd)
            raise
        self._read_fd, write_fd = os.pipe()
        self._error_fd = os.dup(2)
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.dup2(write_fd, 1)
            os.dup2(write_fd, 2)
        finally:
            os.close(write_fd)
        self._thread = threading.Thread(target=self._drain, name="worker-log-writer", daemon=True)
        self._thread.start()
        atexit.register(self.close)

    def _write_all(self, content: bytes) -> None:
        """处理普通文件短写，确保本批内容完整写入。"""
        remaining = memoryview(content)
        while remaining:
            written = os.write(self._log_fd, remaining)
            if written == 0:
                raise OSError("worker 日志写入未取得进展")
            remaining = remaining[written:]

    def _append(self, content: bytes) -> None:
        """超限时原位保留约九成最新内容，避免备份文件继续占用空间。"""
        if len(content) >= self.max_bytes:
            content = _align_tail(content[-self.max_bytes :])
        fcntl.flock(self._log_fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(self._log_fd).st_size
            if size + len(content) > self.max_bytes:
                retained = min(self.max_bytes * 9 // 10, self.max_bytes - len(content))
                tail = os.pread(self._log_fd, retained, size - retained) if retained else b""
                content = _align_tail(tail + content)
                os.ftruncate(self._log_fd, 0)
            self._write_all(content)
        finally:
            fcntl.flock(self._log_fd, fcntl.LOCK_UN)

    def _drain(self) -> None:
        """持续读取标准流和继承描述符的子进程输出，退出时释放文件描述符。"""
        try:
            while content := os.read(self._read_fd, 64 * 1024):
                self._append(content)
        except OSError:
            with suppress(OSError):
                os.write(self._error_fd, "worker 日志写入失败，请检查日志目录空间与权限。\n".encode())
        finally:
            os.close(self._read_fd)
            os.close(self._log_fd)
            os.close(self._error_fd)

    def close(self) -> None:
        """退出时刷新标准流、关闭管道写端，并等待已接收日志写入。"""
        if self._closed:
            return
        self._closed = True
        for stream in (sys.stdout, sys.stderr):
            with suppress(OSError, ValueError):
                stream.flush()
        descriptor = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(descriptor, 1)
            os.dup2(descriptor, 2)
        finally:
            os.close(descriptor)
        self._thread.join(timeout=5)
