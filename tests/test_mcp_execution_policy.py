# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 10:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_execution_policy.py

"""验证 MCP 执行许可独立于恢复能力，结果未知不自动重放。"""

import asyncio
import hashlib
import json
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.main import app  # noqa: F401
from app.models.admin import Admin
from app.models.base import Base
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run, decide_approval
from app.services.worker_tools import make_worker_tool_resolver


class TicketModel(FakeMessagesListChatModel):
    """通过真实运行图申请创建工单，不请求模型供应商。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保留正式工具及审批链路，返回固定模型消息。"""
        return self


@pytest.mark.parametrize("outcome", ["success", "response_lost", "not_sent", "prior_unknown"])
@pytest.mark.parametrize("recovery", ["manual", "idempotent"])
def test_approved_write_executes_once_and_keeps_delivery_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, recovery: str) -> None:
    """无幂等参数的写工具批准后调用一次，响应丢失时记录未知且不补做。"""
    calls = []

    async def invoke(url, name, arguments, token, timeout, expected_schema, **kwargs):
        """在远端副作用发生的位置计数，模拟成功或返回前断线。"""
        assert name == "create_ticket"
        assert arguments["title"] == "修复登录问题"
        if recovery == "manual":
            assert set(arguments) == {"title"}, "普通写不得注入未声明的幂等参数"
        else:
            assert set(arguments) == {"title", "request_id"} and arguments["request_id"]
        if outcome == "not_sent":
            from app.services.tool_delivery import ToolNotSentError

            raise ToolNotSentError("业务调用尚未发送")
        calls.append(dict(arguments))
        if outcome == "response_lost":
            raise ConnectionError("private upstream connection closed")
        return '{"ticket_id": "T-1"}'

    monkeypatch.setattr("app.services.worker_tools.invoke_mcp_tool", invoke)

    async def exercise() -> None:
        """创建隔离数据库，经过申请审批、批准和检查点恢复执行。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp-write.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=1, username="ticket-owner", password="unused", role="user", status=1, is_deleted=0),
                        UserTool(
                            id=3,
                            owner_id=1,
                            name="create_ticket",
                            description="创建一张工单",
                            kind="mcp",
                            visibility="private",
                            enabled=True,
                            current_version=1,
                        ),
                        UserToolVersion(
                            tool_id=3,
                            version=1,
                            content_hash="a" * 64,
                            input_schema={
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                    },
                                },
                                "required": ["title"],
                                "additionalProperties": False,
                            },
                            effect="write",
                            recovery_policy=recovery,
                            idempotency_key_field="request_id" if recovery == "idempotent" else None,
                            requires_approval=True,
                            approval_mode="always",
                            verified=True,
                            write_contract_verified=recovery == "idempotent",
                            mcp_url="https://mcp.example.com/mcp",
                            mcp_tool_name="create_ticket",
                        ),
                    ],
                )
                if recovery == "idempotent":
                    version = next(item for item in db.new if isinstance(item, UserToolVersion))
                    version.input_schema["properties"]["request_id"] = {
                        "type": "string",
                    }
                    version.input_schema["required"].append("request_id")
                run = await create_run(db, 1, input_text="创建一张登录问题工单")
                db.add(RunTool(run_id=run.id, tool_id=3, tool_version=1, content_hash="a" * 64, requires_approval=True))
                if outcome == "prior_unknown":
                    arguments = {
                        "title": "修复登录问题",
                    }
                    db.add(
                        ToolInvocation(
                            run_id=run.id,
                            call_id="previous-unknown-operation",
                            tool_id=3,
                            tool_version=1,
                            state="unknown",
                            arguments_hash=hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest(),
                            failure_code="external_result_unknown",
                        ),
                    )
            model = TicketModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "user_tool_3_create_ticket",
                                "args": {
                                    "title": "修复登录问题",
                                },
                                "id": "create-one-ticket",
                            },
                        ],
                    ),
                    AIMessage(content="已创建工单 T-1"),
                ],
            )
            options = {
                "resolve_tools": make_worker_tool_resolver(sessions),
                "lease_seconds": 10,
                "poll_seconds": 0.05,
            }
            paused = await execute_text_run(sessions, run.id, model, tmp_path / "checkpoints.db", **options)
            assert paused.state == "waiting_for_approval"
            assert calls == []
            async with sessions.begin() as db:
                await decide_approval(db, run.id, 1, "approve")
            result = await execute_text_run(sessions, run.id, model, tmp_path / "checkpoints.db", resume=True, **options)
            assert len(calls) == (0 if outcome in ("not_sent", "prior_unknown") else 1), "批准后的 MCP 写操作未执行，或被重复执行"
            async with sessions() as db:
                invocations = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id))).all()
                assert len(invocations) == 1
                if outcome == "success":
                    assert result.state == "completed", result.failure_code
                    assert invocations[0].state == "completed"
                elif outcome == "not_sent":
                    assert result.state == "failed"
                    assert invocations[0].state == "failed"
                    assert invocations[0].failure_code != "external_result_unknown"
                else:
                    assert result.state == "failed"
                    assert invocations[0].state == "unknown"
                    assert invocations[0].failure_code == "external_result_unknown"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
