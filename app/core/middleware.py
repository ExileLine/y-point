# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : middleware.py

import json
import time
from typing import Iterable, Optional, Union
from urllib.parse import parse_qs, parse_qsl, urlencode

import shortuuid
from loguru import logger
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.response import api_response


SENSITIVE_FIELDS = {
    "password", "passwd", "pwd", "secret", "secretkey", "clientsecret",
    "token", "accesstoken", "refreshtoken", "apikey", "authorization",
    "cookie", "sessionid", "credential", "customheaders",
    "currentpassword", "newpassword", "oldpassword", "passwordconfirmation",
}


class UploadTooLargeError(Exception):
    """上传请求在接收过程中超过总字节上限。"""


class ArtifactUploadLimitMiddleware:
    """在 multipart 解析前限制文件上传请求的实际接收字节数。"""

    def __init__(self, app: ASGIApp, max_bytes: int = 51 * 1024 * 1024):
        """保存下游应用和上传请求总上限。"""
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """拒绝超限请求，包括未声明 Content-Length 的流式上传。"""
        if scope["type"] != "http" or scope.get("path") != "/api/v1/artifacts/uploads" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        headers = _decode_headers(scope.get("headers", []))
        content_length = headers.get("content-length")
        if content_length and content_length.isdecimal() and int(content_length) > self.max_bytes:
            await api_response(http_code=413, code=10005, message="上传请求超过大小限制")(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> dict:
            """逐块累计实际请求字节数。"""
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise UploadTooLargeError()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except UploadTooLargeError:
            await api_response(http_code=413, code=10005, message="上传请求超过大小限制")(scope, receive, send)


def _is_sensitive_field(key: str, sensitive_headers: set) -> bool:
    normalized = "".join(char for char in key.lower() if char.isalnum())
    return normalized in SENSITIVE_FIELDS or normalized in {
        "".join(char for char in header if char.isalnum()) for header in sensitive_headers
    }


def _mask_fields(value, sensitive_headers: set):
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_field(str(key), sensitive_headers)
            else _mask_fields(item, sensitive_headers)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_fields(item, sensitive_headers) for item in value]
    return value


def _decode_headers(raw_headers) -> dict:
    return {k.decode().lower(): v.decode() for k, v in raw_headers}


def _mask_headers(headers: dict, sensitive_headers: set, mask: bool) -> dict:
    if not mask:
        return headers
    return {k: ("***" if k in sensitive_headers else v) for k, v in headers.items()}


def _parse_header_list(value: Union[str, Iterable[str], None]) -> set:
    if not value:
        return set()
    if isinstance(value, str):
        return {h.strip().lower() for h in value.split(",") if h.strip()}
    return {str(h).strip().lower() for h in value if str(h).strip()}


def _get_client_ip(headers: dict, client) -> str:
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if client:
        return client[0]
    return "0.0.0.0"


class RequestLoggingMiddleware:
    def __init__(
            self,
            app: ASGIApp,
            *,
            log_headers: bool = False,
            log_body: bool = False,
            max_body_size: int = 1024 * 1024,
            exclude_paths: Optional[Iterable[str]] = None,
            sensitive_headers: Union[str, Iterable[str], None] = None,
            mask_sensitive_headers: bool = True,
    ):
        self.app = app
        self.log_headers = log_headers
        self.log_body = log_body
        self.max_body_size = max_body_size
        self.exclude_paths = set(exclude_paths or [])
        self.sensitive_headers = _parse_header_list(sensitive_headers)
        self.mask_sensitive_headers = mask_sensitive_headers

    def _is_excluded(self, path: str) -> bool:
        for p in self.exclude_paths:
            if p.endswith("*") and path.startswith(p[:-1]):
                return True
            if path == p:
                return True
        return False

    def _should_read_body(self, method: str, headers: dict) -> bool:
        if not self.log_body:
            return False
        if method in {"GET", "HEAD", "OPTIONS"}:
            return False
        content_type = headers.get("content-type", "").lower()
        if content_type.startswith("multipart/form-data"):
            return False
        try:
            content_length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            return False
        if content_length <= 0 or content_length > self.max_body_size:
            return False
        return True

    async def _receive_body(self, receive: Receive) -> tuple[bytes, bool]:
        body = b""
        disconnected = False

        while True:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
                continue
            if msg_type == "http.disconnect":
                disconnected = True
                break
        return body, disconnected

    def _build_receive(self, body: bytes, original_receive: Receive, disconnected: bool) -> Receive:
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            if disconnected:
                return {"type": "http.disconnect"}
            return await original_receive()

        return receive

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._is_excluded(path):
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        headers = _decode_headers(scope.get("headers", []))
        method = scope.get("method", "")
        query_string = scope.get("query_string", b"").decode(errors="replace")
        client = scope.get("client")
        request_id = headers.get("x-request-id") or headers.get("x-log-uuid") or shortuuid.uuid()
        scope.setdefault("state", {})["request_id"] = request_id

        body = b""
        if self._should_read_body(method, headers):
            body, disconnected = await self._receive_body(receive)
            receive = self._build_receive(body, receive, disconnected)

        status_code = None
        response_headers = []

        async def send_wrapper(message):
            nonlocal status_code, response_headers
            if message["type"] == "http.response.start":
                status_code = message.get("status")
                response_headers = list(message.get("headers", []))
                process_time = time.perf_counter() - start
                response_headers.append((b"x-process-time", f"{process_time:.6f}".encode()))
                if not any(k.lower() == b"x-request-id" for k, _ in response_headers):
                    response_headers.append((b"x-request-id", request_id.encode()))
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            logger.exception("请求处理异常")
            raise
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            query_params = parse_qsl(query_string, keep_blank_values=True)
            masked_query = urlencode([
                (key, "***" if _is_sensitive_field(key, self.sensitive_headers) else value)
                for key, value in query_params
            ])
            full_path = f"{path}?{masked_query}" if query_string else path
            ip = _get_client_ip(headers, client)
            logger.info(
                f"{method} {full_path} {status_code} {elapsed_ms:.2f}ms "
                f"ip={ip} rid={request_id}"
            )
            if self.log_headers:
                logger.info("headers: {}", _mask_headers(headers, self.sensitive_headers, self.mask_sensitive_headers))
            if self.log_body and body:
                content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type == "application/json" or content_type.endswith("+json"):
                    try:
                        body_data = _mask_fields(json.loads(body), self.sensitive_headers)
                    except (ValueError, UnicodeDecodeError):
                        body_data = "[invalid JSON omitted]"
                elif content_type == "application/x-www-form-urlencoded":
                    body_data = _mask_fields(
                        parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True),
                        self.sensitive_headers,
                    )
                else:
                    body_data = "[body omitted]"
                logger.info("body: {}", body_data)


MyMiddleware = RequestLoggingMiddleware
