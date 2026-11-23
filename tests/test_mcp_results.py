# -*- coding: utf-8 -*-
# @Time    : 2026-11-23
# @Author  : Codex
# @File    : test_mcp_results.py

"""通过私有存储、正式 API 和真实 MCP 协议验证大结果交付。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import hashlib
import json
import socket

import httpx
from langchain_core.messages import AIMessage
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
import pytest
from sqlalchemy import select
import uvicorn

from app.core.config import get_config
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.models.tool import RunTool, UserTool, UserToolVersion, ToolInvocation
from app.services import mcp_tools
from app.services.mcp_results import deliver_mcp_result, read_result_page, redact_result
from app.services.provider_credentials import encrypt_credentials
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_manager import create_run, decide_approval, request_cancel
from app.services.tool_execution import CURRENT_TOOL_CALL_ID
from app.services.tool_registry import ToolContext
from app.services.worker_tools import make_worker_tool_resolver
from tests.test_mcp_approval_continue import Model
from tests.test_mcp_run_sessions import database, running


def response():
    """一万条记录只代表本次响应，不能推断远端数据集没有下一页。"""
    return {
        "isError": False,
        "content": [],
        "structuredContent": {
            "rows": [
                {
                    "id": i,
                    "name": "中文记录😀",
                }
                for i in range(10000)
            ],
            "nextCursor": "remote-page-2",
            "password": "private-password",
        },
    }


@pytest.mark.parametrize("failure", [None, "quota", "decoded", "count", "storage"])
def test_result_snapshot_preview_and_storage_failure_keep_success(tmp_path, monkeypatch, failure):
    """保存、配额及序列化预算影响交付，不把成功操作伪装为业务失败。"""

    async def exercise():
        monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "private"))
        if failure == "quota":
            monkeypatch.setattr(get_config(), "ARTIFACT_OWNER_MAX_BYTES", 100)
        if failure == "decoded":
            monkeypatch.setattr(get_config(), "MCP_RESULT_MAX_BYTES", 1024)
        if failure == "count":
            monkeypatch.setattr(get_config(), "MCP_RESULT_MAX_PER_RUN", 0)
        if failure == "storage":

            async def broken(*args, **kwargs):
                """只模拟存储故障，不替换远端成功响应。"""
                raise OSError("private-storage-error")

            monkeypatch.setattr("app.services.mcp_results.stage_generated_artifact", broken)
        async with database(tmp_path / "results.db") as sessions:
            run = await running(sessions)
            context = ToolContext(sessions, run.id, 1, run.lease_token)
            marker = CURRENT_TOOL_CALL_ID.set("confirmed-large-call")
            try:
                result = json.loads(await deliver_mcp_result(response(), context, tool_name="query", config="http://unused", credentials=None))
                assert result["operation_succeeded"] is True
                assert result["preview_complete"] is False
                assert len(json.dumps(result["preview"], ensure_ascii=False).encode()) <= get_config().MCP_RESULT_PREVIEW_BYTES
                assert "private-" not in json.dumps(result)
                async with sessions() as db:
                    items = (await db.scalars(select(Artifact))).all()
                if failure:
                    assert result["delivery"] == "unavailable" and not items
                    assert "artifact_id" not in result
                    return
                assert result["delivery"] == "stored" and len(items) == 1
                assert items[0].state == "ready" and items[0].tool_result["remote_dataset_complete"] is None
                full = []
                offset = 0
                async with sessions() as db:
                    while True:
                        page = await read_result_page(db, result["artifact_id"], 1, offset=offset, expected_sha256=result["sha256"], run_id=run.id)
                        full.append(page["text_fragment"])
                        if page["next_offset"] is None:
                            break
                        offset = page["next_offset"]
                    reconstructed = "".join(full)
                    assert hashlib.sha256(reconstructed.encode()).hexdigest() == result["sha256"]
                    data = json.loads(reconstructed)
                    assert len(data["structuredContent"]["rows"]) == 10000
                    assert data["structuredContent"]["password"] == "[已脱敏]"
                    assert data["structuredContent"]["nextCursor"] == "remote-page-2"
                    with pytest.raises(ValueError):
                        await read_result_page(db, result["artifact_id"], 2)
                    with pytest.raises(ValueError):
                        await read_result_page(db, result["artifact_id"], 1, run_id="other-run")
                    with pytest.raises(ValueError):
                        await read_result_page(db, result["artifact_id"], 1, expected_sha256="0" * 64)
                    with pytest.raises(ValueError):
                        await read_result_page(db, result["artifact_id"], 1, offset=len(reconstructed) + 1)
            finally:
                CURRENT_TOOL_CALL_ID.reset(marker)

    asyncio.run(exercise())


def test_result_redacts_nested_json_and_credentials():
    """连接凭据可能被远端回显在普通字段，嵌入 JSON 的敏感字段仍应过滤。"""
    value = {
        "content": [
            {
                "text": json.dumps(
                    {
                        "X-API-Key": "another-secret",
                        "value": "actual-connection-secret",
                        "headers": {
                            "x": "secret",
                        },
                    }
                ),
            }
        ],
        "note": "password=another-secret",
        "actual-connection-secret": "secret-key-name",
    }
    text = json.dumps(redact_result(value, secrets=["actual-connection-secret"]))
    assert "another-secret" not in text and "actual-connection-secret" not in text
    assert "[" in text


def test_result_api_expiry_integrity_cancellation_and_owner(tmp_path, monkeypatch):
    """真实 API 按页重验授权，取消后模型不能读取，但所有者仍能核实已经成功的响应。"""

    async def exercise():
        monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "private"))
        async with database(tmp_path / "api.db") as sessions:
            run = await running(sessions)
            context = ToolContext(sessions, run.id, 1, run.lease_token)
            marker = CURRENT_TOOL_CALL_ID.set("api-call")
            try:
                result = json.loads(
                    await deliver_mcp_result(response(), context, tool_name="query", config="http://unused", credentials=encrypt_credentials("credential", {}))
                )
            finally:
                CURRENT_TOOL_CALL_ID.reset(marker)

            async def db_override():
                """正式 API 使用隔离数据库。"""
                async with sessions() as db:
                    yield db

            actor = Admin(id=1, username="owner")
            app.dependency_overrides[get_db_session] = db_override
            app.dependency_overrides[get_current_admin] = lambda: actor
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    url = f"/api/v1/artifacts/{result['artifact_id']}/result-page"
                    page = await client.get(
                        url,
                        params={
                            "expected_sha256": result["sha256"],
                        },
                    )
                    assert page.status_code == 200 and page.json()["data"]["next_offset"] > 0
                    assert "storage_key" not in page.text and "private-password" not in page.text
                    actor.id = 2
                    assert (
                        await client.get(
                            url,
                            params={
                                "expected_sha256": result["sha256"],
                            },
                        )
                    ).status_code == 404
                    actor.id = 1
                    assert (
                        await client.get(
                            url,
                            params={
                                "expected_sha256": "0" * 64,
                            },
                        )
                    ).status_code == 409
                    async with sessions.begin() as db:
                        await request_cancel(db, run.id, 1)
                    from app.services.mcp_results import result_reader_tool

                    with pytest.raises(ValueError):
                        await result_reader_tool(context).ainvoke(
                            {
                                "artifact_id": result["artifact_id"],
                                "expected_sha256": result["sha256"],
                            }
                        )
                    assert (
                        await client.get(
                            url,
                            params={
                                "expected_sha256": result["sha256"],
                            },
                        )
                    ).status_code == 200
                    async with sessions.begin() as db:
                        item = await db.get(Artifact, result["artifact_id"])
                        item.status = 0
                    assert (await client.get(f"/api/v1/artifacts/{result['artifact_id']}/download")).status_code == 404
                    assert (await client.get("/api/v1/artifacts", params={"run_id": run.id})).json()["data"] == []
                    async with sessions.begin() as db:
                        item = await db.get(Artifact, result["artifact_id"])
                        item.status = 1
                        item.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
                    assert (
                        await client.get(
                            url,
                            params={
                                "expected_sha256": result["sha256"],
                            },
                        )
                    ).status_code == 404
            finally:
                app.dependency_overrides.clear()

    asyncio.run(exercise())


@asynccontextmanager
async def remote_server(monkeypatch, *, error=False):
    """真实 TCP + 官方 MCP 服务器；记录业务调用次数。"""
    remote = FastMCP("large-results", json_response=True, transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    calls = []

    @remote.tool(annotations=ToolAnnotations(readOnlyHint=False))
    async def create_orders() -> dict:
        """模拟一次成功创建并返回大量行。"""
        calls.append(1)
        if error:
            raise ValueError("private-remote-error")
        return response()["structuredContent"]

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(remote.streamable_http_app(), log_level="critical", lifespan="on", timeout_graceful_shutdown=0.5))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    async with asyncio.timeout(5):
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
    monkeypatch.setattr(mcp_tools, "validate_mcp_url", lambda url: "127.0.0.1")
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/mcp", calls
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.mark.parametrize("mode", ["stored", "save_failed", "remote_error", "transport_limit", "delivery_timeout"])
def test_formal_worker_large_write_success_or_unknown_without_replay(tmp_path, monkeypatch, mode):
    """正式审批/图/Worker 与真实协议：保存失败仍完成，远端错误或传输中断仍 UNKNOWN。"""

    async def exercise():
        monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "private"))
        monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 4 * 1024 * 1024)
        if mode == "save_failed":
            monkeypatch.setattr(get_config(), "ARTIFACT_OWNER_MAX_BYTES", 100)
        if mode == "delivery_timeout":

            async def slow_storage(*args, **kwargs):
                """远端已成功，随后结果文件暂存超过外层工具时限。"""
                await asyncio.sleep(5)

            monkeypatch.setattr("app.services.mcp_results.stage_generated_artifact", slow_storage)
        async with database(tmp_path / "worker.db") as sessions, remote_server(monkeypatch, error=mode == "remote_error") as (url, calls):
            remote = (await mcp_tools.discover_mcp_tools(url))[0]
            async with sessions.begin() as db:
                db.add(
                    UserTool(id=9, owner_id=1, name="large_write", description="创建订单", kind="mcp", visibility="private", enabled=True, current_version=1)
                )
                db.add(
                    UserToolVersion(
                        tool_id=9,
                        version=1,
                        content_hash="9" * 64,
                        input_schema=remote["input_schema"],
                        effect="write",
                        recovery_policy="manual",
                        requires_approval=True,
                        verified=True,
                        mcp_url=url,
                        mcp_tool_name="create_orders",
                    )
                )
                run = await create_run(db, 1, input_text="创建一次订单并说明结果", tool_timeout_seconds=1 if mode == "delivery_timeout" else 120)
                db.add(RunTool(run_id=run.id, tool_id=9, tool_version=1, content_hash="9" * 64, requires_approval=True))
            if mode == "transport_limit":
                monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 8192)
            model = Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "user_tool_9_large_write",
                                "args": {},
                                "id": "once",
                            }
                        ],
                    ),
                    AIMessage(content="已经处理，不重做操作"),
                ]
            )

            async def resolve_model(db, run):
                """上游模型受控，其余使用正式执行链路。"""
                return model

            resolver = make_worker_tool_resolver(sessions)
            paused = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=resolve_model, resolve_tools=resolver)
            assert paused.state == "waiting_for_approval"
            async with sessions.begin() as db:
                await decide_approval(db, run.id, 1, "approve")
            result = await dispatch_next_run(sessions, tmp_path / "checkpoint.db", resolve_model=resolve_model, resolve_tools=resolver)
            assert calls == [1]
            async with sessions() as db:
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
                items = (await db.scalars(select(Artifact).where(Artifact.run_id == run.id))).all()
            if mode in {"remote_error", "transport_limit"}:
                assert result.state in {"failed", "timed_out"} and invocation.state == "unknown"
                assert not items
            elif mode == "delivery_timeout":
                assert result.state == "timed_out" and invocation.state == "completed" and invocation.failure_code is None
                from app.services.run_calls import load_run_calls

                async with sessions() as db:
                    public_calls = (await load_run_calls(db, 1, [run.id]))[run.id]
                assert public_calls[0].status == "succeeded" and public_calls[0].failure_code is None
                assert not items
            else:
                assert result.state == "completed" and invocation.state == "completed"
                assert len(items) == (1 if mode == "stored" else 0)

    asyncio.run(exercise())


@pytest.mark.parametrize("backend", ["local", "s3"])
def test_result_quota_concurrency_and_expired_blob_cleanup(tmp_path, monkeypatch, backend):
    """并发结果不能突破每 Run 配额；到期后沿用产物清理删除私有正文。"""

    async def exercise():
        from app.services.artifact_store import artifact_root, prune_artifacts

        monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "private"))
        monkeypatch.setattr(get_config(), "MCP_RESULT_MAX_PER_RUN", 1)
        s3 = None
        if backend == "s3":
            from tests.test_private_blob_store import MemoryS3
            from app.services import private_blob_store

            s3 = MemoryS3()
            monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_BACKEND", "s3")
            monkeypatch.setattr(get_config(), "ARTIFACT_S3_BUCKET", "private-test")
            monkeypatch.setattr(private_blob_store.boto3, "client", lambda *args, **kwargs: s3)
        async with database(tmp_path / "quota.db") as sessions:
            run = await running(sessions)
            context = ToolContext(sessions, run.id, 1, run.lease_token)

            async def deliver(call_id):
                """同一轮两项已成功的响应竞争一个留存位置。"""
                marker = CURRENT_TOOL_CALL_ID.set(call_id)
                try:
                    return json.loads(await deliver_mcp_result(response(), context, tool_name="query", config="http://unused", credentials=None))
                finally:
                    CURRENT_TOOL_CALL_ID.reset(marker)

            results = await asyncio.gather(deliver("one"), deliver("two"))
            assert all(item["operation_succeeded"] for item in results)
            assert sum(item["delivery"] == "stored" for item in results) == 1
            async with sessions.begin() as db:
                artifacts = (await db.scalars(select(Artifact))).all()
                assert len(artifacts) == 1
                artifact = artifacts[0]
                key = artifact.storage_key
                artifact.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            expired, _ = await prune_artifacts(sessions, artifact_root())
            assert expired == 1
            assert not (artifact_root() / key).exists()
            if s3 is not None:
                assert not s3.objects and s3.encryption and set(s3.encryption) == {"AES256"}
            async with sessions() as db:
                with pytest.raises(ValueError):
                    await read_result_page(db, artifact.id, 1)

    asyncio.run(exercise())
