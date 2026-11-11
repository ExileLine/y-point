# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 13:01:08
# @Author  : yangyuexiong
# @File    : test_model_connection_retry.py

"""使用真实本地 TCP 连接验证模型响应前恢复与流式中断边界。"""

import asyncio
import json
import socket
import struct
from contextlib import suppress
from pathlib import Path

import pytest
import httpx2
from openai import APIConnectionError, APITimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.model_adapter import build_chat_model
from app.services.model_connection_retry import invoke_model_with_connection_retry
from app.services.run_manager import claim_run, create_run


@pytest.mark.parametrize("failure_phase", ["before_headers", "empty_chunk", "text_chunk"])
def test_sdk_tcp_reset_retry_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_phase: str) -> None:
    """让真实 SDK 遇到 TCP reset，确认仅响应前断连重试且逐次计数，部分流不会重发。"""
    monkeypatch.setattr(get_config(), "AGENT_MODEL_CONNECTION_MAX_RETRIES", 1)
    monkeypatch.setattr(get_config(), "AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS", 0)

    async def exercise() -> None:
        """在本地 HTTP 服务注入实际断连，不使用外部模型或认证信息。"""
        requests = []
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sdk-reset.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """首次请求按阶段复位连接，后续请求返回符合协议的完整 SSE 回复。"""
            try:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
                length = next(int(line.split(":", 1)[1]) for line in headers.split("\r\n") if line.lower().startswith("content-length:"))
                body = json.loads(await reader.readexactly(length))
                assert body["stream"] is True
                requests.append(1)
                if len(requests) == 1 and failure_phase == "before_headers":
                    writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    writer.transport.abort()
                    return
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
                chunk = {
                    "id": "chatcmpl-local-reset",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "local-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "" if failure_phase == "empty_chunk" and len(requests) == 1 else "OK",
                            },
                            "finish_reason": None,
                        },
                    ],
                }
                writer.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                await writer.drain()
                if len(requests) == 1:
                    await asyncio.sleep(0.02)
                    writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    writer.transport.abort()
                    return
                chunk["choices"][0]["delta"] = {}
                chunk["choices"][0]["finish_reason"] = "stop"
                writer.write(b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n")
                await writer.drain()
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="local-reset-owner", password="unused", role="user"))
                run = await create_run(db, 1)
                run = await claim_run(db, run.id)
            async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                provider = ProviderConnection(id=1, kind="openai_compatible", base_url=f"http://127.0.0.1:{port}/v1", timeout_seconds=2, status=1, is_deleted=0)
                catalog = ModelCatalog(id=1, provider_id=1, external_id="local-model", status=1, is_deleted=0)
                model = build_chat_model(catalog, provider, max_output_tokens=64)

                async def invoke():
                    """由正式适配器发出真实流式 HTTP 请求。"""
                    return await model.ainvoke("只回复 OK", stream=True)

                try:
                    async with asyncio.timeout(5):
                        if failure_phase == "before_headers":
                            result = await invoke_model_with_connection_retry(invoke, sessions, run.id, run.lease_token)
                            assert result.content == "OK"
                        else:
                            with pytest.raises((httpx2.ReadError, httpx2.RemoteProtocolError, APIConnectionError)) as error:
                                await invoke_model_with_connection_retry(invoke, sessions, run.id, run.lease_token)
                            assert not isinstance(error.value, APITimeoutError)
                finally:
                    await model.async_client._client.close()
            expected = 2 if failure_phase == "before_headers" else 1
            assert len(requests) == expected
            async with sessions() as db:
                saved = await db.get(AgentRun, run.id)
                assert saved.model_call_count == expected
        finally:
            await engine.dispose()

    asyncio.run(exercise())
