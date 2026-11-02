# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 22:54:10
# @Author  : yangyuexiong
# @File    : package_proxy.py

"""依赖下载容器的 HTTPS 出口代理，仅允许维护者配置的包仓库域名。"""

import json
import os
import select
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore

ALLOWED_HOSTS = frozenset(host.lower().rstrip(".") for host in json.loads(os.environ["PACKAGE_ALLOWED_HOSTS"]))
CONNECTIONS = BoundedSemaphore(16)


class PackageProxy(BaseHTTPRequestHandler):
    """在隔离 Docker 网络内提供受限的 HTTPS CONNECT 隧道。"""

    def log_message(self, format: str, *args) -> None:
        """不记录仓库请求路径、查询参数或认证信息。"""

    def do_CONNECT(self) -> None:
        """只转发允许目标的 HTTPS 连接，拒绝未配置目标和其他端口。"""
        host, separator, port = self.path.rpartition(":")
        host = host.lower().rstrip(".")
        if not separator or port != "443" or host not in ALLOWED_HOSTS:
            self.send_error(403, "Package destination is not allowed")
            return
        if not CONNECTIONS.acquire(blocking=False):
            self.send_error(503, "Too many package connections")
            return
        try:
            with socket.create_connection((host, 443), timeout=15) as remote:
                self.send_response(200, "Connection Established")
                self.end_headers()
                self.connection.setblocking(False)
                remote.setblocking(False)
                peers = (self.connection, remote)
                while True:
                    readable, _, errors = select.select(peers, (), peers, 30)
                    if not readable or errors:
                        return
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        destination = remote if source is self.connection else self.connection
                        # 有界缓冲发送，慢连接最长等待 15 秒。
                        remaining = memoryview(data)
                        while remaining:
                            if not select.select((), (destination,), (), 15)[1]:
                                return
                            sent = destination.send(remaining)
                            if not sent:
                                return
                            remaining = remaining[sent:]
        except OSError:
            return
        finally:
            CONNECTIONS.release()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8765), PackageProxy).serve_forever()
