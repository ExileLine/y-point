"""验证 MCP 只读错误反馈、参数修正及运行失败分类。"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from mcp.types import CallToolResult, TextContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_run_event import AgentRunEvent
from app.models.base import Base
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services import mcp_tools
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run
from app.services.run_calls import load_run_calls
from app.services.worker_tools import make_worker_tool_resolver

SCHEMA = {
    "type": "object",
    "properties": {
        "libraryId": {
            "type": "string",
        },
    },
    "required": ["libraryId"],
}
REDIRECT = "Library /old/library has been redirected to this library: /websites/fastapi_tiangolo."


def mock_mcp(monkeypatch: pytest.MonkeyPatch, mode: str, calls: list) -> None:
    """替换连接和 SDK 会话，保留生产调用、错误转换与图执行路径。"""

    @asynccontextmanager
    async def connection(*args):
        """提供不连接网络的上下文。"""
        async with asyncio.TaskGroup():
            yield None, None, None

    class Session:
        async def __aenter__(self):
            """打开测试会话。"""
            return self

        async def __aexit__(self, *args):
            """关闭测试会话。"""

        async def initialize(self):
            """模拟已初始化。"""

        async def call_tool(self, name, arguments, **kwargs):
            """模拟库迁移、错误大响应、连接拒绝或正常查询。"""
            calls.append(arguments["libraryId"])
            if mode == "http":
                raise httpx.HTTPStatusError("private-upstream", request=httpx.Request("POST", "https://private.invalid"), response=httpx.Response(503))
            error = arguments["libraryId"] == "/old/library"
            text = "private-remote-" * 2000 if mode == "large" else REDIRECT if error else "FastAPI documentation"
            return CallToolResult(isError=error, content=[TextContent(type="text", text=text)])

    async def discover(session):
        """返回固定只读契约。"""
        return [
            {
                "name": "query-docs",
                "input_schema": SCHEMA,
                "read_only": True,
            },
        ]

    monkeypatch.setattr(mcp_tools, "_mcp_connection", connection)
    monkeypatch.setattr(mcp_tools, "ClientSession", lambda *args, **kwargs: Session())
    monkeypatch.setattr(mcp_tools, "_discover_session", discover)


@pytest.mark.parametrize("mode", ["redirect", "http"])
def test_mcp_failure_in_real_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """只读库迁移可由模型修正；真正的工具传输失败不再标记为模型失败。"""
    calls = []
    mock_mcp(monkeypatch, mode, calls)
    observed = []

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            """绑定正式 worker 注册的工具。"""
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            """确认模型看到了错误消息与迁移目标，再允许返回纠正后的调用。"""
            feedback = [m for m in messages if isinstance(m, ToolMessage) and m.status == "error"]
            if feedback:
                payload = json.loads(feedback[-1].content)
                assert payload["error"] == "remote_error"
                assert REDIRECT in feedback[-1].content
                observed.append(payload)
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def exercise():
        """在隔离数据库与检查点中执行真实 MCP 工具链。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mcp.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all([
                    Admin(id=1, username="mcp-owner", password="unused", status=1, is_deleted=0),
                    UserTool(id=3, owner_id=1, name="context7", description="查询文档", kind="mcp", visibility="private", current_version=1, enabled=True),
                    UserToolVersion(
                        tool_id=3, version=1, content_hash="a" * 64, input_schema=SCHEMA, effect="read", recovery_policy="safe",
                        requires_approval=False, verified=True, mcp_url="https://mcp.example.com/mcp", mcp_tool_name="query-docs",
                    ),
                ])
                run = await create_run(db, 1, input_text="查询文档")
                db.add(RunTool(run_id=run.id, tool_id=3, tool_version=1, content_hash="a" * 64, requires_approval=False))
            model = Model(responses=[
                AIMessage(content="", tool_calls=[{
                    "name": "user_tool_3_context7",
                    "args": {
                        "libraryId": "/old/library",
                    },
                    "id": "old-call",
                }]),
                AIMessage(content="", tool_calls=[{
                    "name": "user_tool_3_context7",
                    "args": {
                        "libraryId": "/websites/fastapi_tiangolo",
                    },
                    "id": "corrected-call",
                }]),
                AIMessage(content="查询成功"),
            ])
            result = await execute_text_run(
                sessions, run.id, model, tmp_path / "checkpoints.db", resolve_tools=make_worker_tool_resolver(sessions), lease_seconds=10, poll_seconds=0.05,
            )
            async with sessions() as db:
                invocations = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id).order_by(ToolInvocation.id))).all()
                events = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run.id))).all()
                public_calls = (await load_run_calls(db, 1, [run.id]))[run.id]
                public = json.dumps([c.model_dump(mode="json") for c in public_calls]) + str([e.to_dict() for e in events])
                assert all(secret not in public for secret in (REDIRECT, "/old/library", "private-upstream", "private.invalid"))
                assert invocations[0].state == "failed" and invocations[0].failure_code == "remote_error"
                if mode == "http":
                    assert result.state == "failed" and result.failure_code == "tool_error"
                    assert calls == ["/old/library"]
                else:
                    assert result.state == "completed" and result.failure_code is None
                    assert calls == ["/old/library", "/websites/fastapi_tiangolo"]
                    assert observed and invocations[1].state == "completed"
                    assert [c.status for c in public_calls] == ["failed", "succeeded"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("read_only,mode", [(False, "redirect"), (True, "large")])
def test_mcp_write_errors_still_stop_and_read_feedback_is_bounded(monkeypatch: pytest.MonkeyPatch, read_only: bool, mode: str) -> None:
    """写工具不转换为可继续的反馈，过大远端错误不透传到模型。"""
    calls = []
    mock_mcp(monkeypatch, mode, calls)

    async def exercise():
        """只读反馈保持有界，写工具沿用终止语义。"""
        from app.services.tool_arguments import MCPReadToolError

        with pytest.raises(MCPReadToolError if read_only else ValueError) as failure:
            await mcp_tools.invoke_mcp_tool(
                "https://mcp.example.com/mcp", "query-docs", {
                    "libraryId": "/old/library",
                }, None, 10, SCHEMA, expected_read_only=read_only,
            )
        if read_only:
            payload = json.loads(str(failure.value))
            assert payload["truncated"] is True and "remote_result" not in payload
            assert "private-remote" not in str(failure.value)
            assert len(str(failure.value).encode()) < 16 * 1024
        assert calls == ["/old/library"]

    asyncio.run(exercise())
