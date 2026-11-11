# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 14:48:07
# @Author  : yangyuexiong
# @File    : test_model_context_errors.py

"""验证真实 SDK 的上下文拒绝可区分于其他请求错误且不会重发。"""

import asyncio
import json
from contextlib import suppress
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_run_event import AgentRunEvent
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.model_adapter import build_chat_model
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run


@pytest.mark.parametrize(
    ("error_body", "expected"),
    [
        (
            {
                "message": (
                    "This model's maximum context length is 65536 tokens. However, you requested 32768 output tokens "
                    "and your prompt contains at least 32769 input tokens. Please reduce the length of the input prompt "
                    "or the number of requested output tokens. private-context-marker"
                ),
                "param": "input_tokens",
                "code": 400,
            },
            "context_window_exceeded",
        ),
        (
            {
                "error": {
                    "message": "private-context-marker",
                    "code": "context_length_exceeded",
                },
            },
            "context_window_exceeded",
        ),
        (
            {
                "message": "Unsupported parameter: max_completion_tokens. private-context-marker",
                "param": "max_completion_tokens",
                "code": 400,
            },
            "model_error",
        ),
        (
            {
                "message": "Invalid input_tokens parameter. private-context-marker",
                "param": "input_tokens",
                "code": 400,
            },
            "model_error",
        ),
    ],
    ids=["vllm-context-window", "standard-context-code", "unsupported-output-option", "unrelated-input-option"],
)
@pytest.mark.parametrize("output_limit", [32768, None])
def test_sdk_context_rejection_is_classified_without_retry(tmp_path: Path, error_body: dict, expected: str, output_limit: int | None) -> None:
    """将真实服务的 400 文案送入 SDK 和完整执行器，验证原因码、请求次数和错误正文不落库。"""

    async def exercise() -> None:
        """用本地 HTTP 服务重放供应商拒绝，避免依赖外部模型。"""
        requests = []
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'context-errors.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)

        async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            """接收完整请求并返回固定的供应商错误，不输出请求中的正文。"""
            try:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode("latin1")
                length = next(int(line.split(":", 1)[1]) for line in headers.split("\r\n") if line.lower().startswith("content-length:"))
                payload = json.loads(await reader.readexactly(length))
                if output_limit is None:
                    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & payload.keys()
                requests.append(1)
                body = json.dumps(error_body).encode()
                writer.write(
                    b"HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body,
                )
                await writer.drain()
            finally:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="context-owner", password="unused", role="user"))
                run = await create_run(db, 1, input_text="审核文档", agent_max_output_tokens=output_limit)
            async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                provider = ProviderConnection(id=1, kind="openai_compatible", base_url=f"http://127.0.0.1:{port}/v1", timeout_seconds=2, status=1, is_deleted=0)
                catalog = ModelCatalog(id=1, provider_id=1, external_id="context-model", status=1, is_deleted=0)
                model = build_chat_model(catalog, provider, max_output_tokens=output_limit)
                try:
                    async with asyncio.timeout(10):
                        result = await execute_text_run(sessions, run.id, model, tmp_path / "checkpoint.db")
                finally:
                    await model.async_client._client.close()
            assert result.state == "failed" and result.failure_code == expected
            assert result.model_call_count == 1 and result.tool_call_count == 0 and len(requests) == 1
            async with sessions() as db:
                events = list(await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id)))
                assert "private-context-marker" not in repr([event.__dict__ for event in events])
        finally:
            await engine.dispose()

    asyncio.run(exercise())
