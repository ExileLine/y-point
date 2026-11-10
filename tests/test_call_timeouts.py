# -*- coding: utf-8 -*-
# @Time    : 2026-11-09 21:59:55
# @Author  : yangyuexiong
# @File    : test_call_timeouts.py

"""验证分类调用时限、MCP 完整生命周期及知识检索请求的取消。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp import McpError
from mcp.types import ErrorData

from app.api.v1 import knowledge as knowledge_api
from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import ToolKind
from app.schemas.knowledge import KnowledgeSearchIn
from app.services import knowledge_tool, mcp_tools
from app.services.call_timeouts import effective_tool_timeout
from app.services.embedding_adapter import embed_texts
from app.services.knowledge_parser import KnowledgeProcessingError
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.core.enums import KnowledgeFailure, ProviderKind


def test_category_timeouts_use_agent_and_deployment_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """分类时限与 Agent 时限取较小值，MCP 不再被脚本的 30 秒预算截断。"""
    config = get_config()
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 300)
    monkeypatch.setattr(config, "SCRIPT_EXECUTION_TIMEOUT_SECONDS", 90)
    monkeypatch.setattr(config, "KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 80)
    assert effective_tool_timeout(600, kind=ToolKind.MCP) == 300
    assert effective_tool_timeout(120, kind=ToolKind.MCP) == 120
    assert effective_tool_timeout(600, kind=ToolKind.SCRIPT) == 90
    assert effective_tool_timeout(45, kind=ToolKind.SCRIPT) == 45
    assert effective_tool_timeout(600, tool_name="search_knowledge_base") == 80
    assert effective_tool_timeout(40, tool_name="search_knowledge_base") == 40
    assert effective_tool_timeout(600, tool_name="read_file") == 600


@pytest.mark.parametrize("mode", ["success", "initialize", "discover", "invoke", "combined"])
def test_mcp_call_deadline_covers_all_phases(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    """连接、初始化、结构复核和远端调用共享一次预算，任何阶段卡住都会关闭连接。"""
    config = get_config()
    monkeypatch.setattr(config, "MCP_CALL_TIMEOUT_SECONDS", 0.12)
    phases = []
    session_closed = []
    connection_closed = []
    timeouts = []
    schema = {
        "type": "object",
    }

    async def phase(name: str) -> None:
        """模拟异步远端响应，组合场景每阶段未超时但总和超出单次 MCP 预算。"""
        phases.append(name)
        await asyncio.sleep(0.05 if mode == "combined" else 0.3 if mode == name else 0)

    @asynccontextmanager
    async def connection(url, token, read_timeout):
        """模拟 MCP 传输生命周期，仅验证正式客户端入口的计时和资源清理。"""
        timeouts.append(read_timeout)
        try:
            yield None, None, None
        finally:
            connection_closed.append(True)

    class Session:
        """保留 SDK 接口和请求超时参数的本地替身。"""

        def __init__(self, read, write, *, read_timeout_seconds):
            """确认握手和分页请求也配置了会话默认超时。"""
            assert read_timeout_seconds == timedelta(seconds=0.12)

        async def __aenter__(self):
            """进入会话。"""
            return self

        async def __aexit__(self, *args):
            """超时及正常结束均关闭会话。"""
            session_closed.append(True)

        async def initialize(self):
            """模拟协议初始化。"""
            await phase("initialize")

        async def call_tool(self, name, *, arguments, read_timeout_seconds):
            """模拟真正工具调用，并确认请求时限与会话预算一致。"""
            assert read_timeout_seconds == timedelta(seconds=0.12)
            await phase("invoke")
            return SimpleNamespace(
                isError=False,
                model_dump=lambda **kwargs: {
                    "isError": False,
                    "content": [],
                },
            )

    async def discover(session):
        """模拟正式调用前的工具结构复核。"""
        await phase("discover")
        return [
            {
                "name": "review",
                "input_schema": schema,
                "read_only": True,
            },
        ]

    monkeypatch.setattr(mcp_tools, "_mcp_connection", connection)
    monkeypatch.setattr(mcp_tools, "ClientSession", Session)
    monkeypatch.setattr(mcp_tools, "_discover_session", discover)

    async def exercise() -> None:
        """从实际 MCP 调用服务入口验证成功及各阶段超时。"""
        if mode == "success":
            assert '"isError": false' in await mcp_tools.invoke_mcp_tool("https://mcp.example/mcp", "review", {}, None, 300, schema)
        else:
            with pytest.raises(TimeoutError):
                await mcp_tools.invoke_mcp_tool("https://mcp.example/mcp", "review", {}, None, 300, schema)
        assert timeouts == [0.12] and connection_closed == [True] and session_closed == [True]
        if mode == "initialize":
            assert phases == ["initialize"]
        if mode == "combined":
            assert phases == ["initialize", "discover", "invoke"]

    asyncio.run(exercise())


def test_mcp_discovery_and_sdk_timeout_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    """发现也受单次时限保护，并将 SDK/任务组包装超时保持为可识别的工具超时。"""
    monkeypatch.setattr(get_config(), "MCP_DISCOVERY_TIMEOUT_SECONDS", 0.03)
    closed = []

    @asynccontextmanager
    async def stalled_connection(url, token, read_timeout):
        """模拟建连阶段停滞，不启动真实网络请求。"""
        assert read_timeout == 0.03
        try:
            await asyncio.sleep(1)
            yield None, None, None
        finally:
            closed.append(True)

    monkeypatch.setattr(mcp_tools, "_mcp_connection", stalled_connection)

    async def exercise() -> None:
        """验证总时限及 SDK 标准超时均进入同一可操作类别。"""
        with pytest.raises(TimeoutError):
            await mcp_tools.discover_mcp_tools("https://mcp.example/mcp")
        assert closed == [True]
        for error in (
            httpx.ReadTimeout("private-detail"),
            McpError(ErrorData(code=408, message="private-detail")),
            ExceptionGroup("private-group", [TimeoutError("private-detail")]),
        ):
            with pytest.raises(TimeoutError, match="MCP 单次操作超时"):
                async with mcp_tools._mcp_deadline(1):
                    raise error
        with pytest.raises(ValueError, match="schema changed"):
            async with mcp_tools._mcp_deadline(1):
                raise ValueError("schema changed")

    asyncio.run(exercise())


def test_knowledge_api_and_agent_use_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 知识检索和 Agent 检索使用同一部署限额，并取消慢调用。"""
    monkeypatch.setattr(get_config(), "KNOWLEDGE_SEARCH_TIMEOUT_SECONDS", 0.03)
    stopped = []
    leases = []

    async def slow_search(*args, **kwargs):
        """模拟仍在等待的知识查询，确认超时确实取消执行。"""
        try:
            await asyncio.sleep(1)
        finally:
            stopped.append(True)

    @asynccontextmanager
    async def slot(sessions, owner, run, kind, timeout):
        """记录请求所用资源预算与退出，避免将固定 60 秒遗留在并发租约。"""
        leases.append(timeout)
        try:
            yield "test-lease"
        finally:
            leases.append("released")

    class Sessions:
        """仅提供 Agent 固定代查询所需的读取接口。"""

        async def __aenter__(self):
            """进入只读上下文。"""
            return self

        async def __aexit__(self, *args):
            """退出只读上下文。"""

        async def scalars(self, query):
            """本用例只验证时限，不伪造真实检索命中。"""
            return SimpleNamespace(all=lambda: [])

    monkeypatch.setattr(knowledge_api, "require_knowledge_base", AsyncMock(return_value=SimpleNamespace(active_index_id=1)))
    monkeypatch.setattr(knowledge_api, "async_sessionmaker", lambda *args, **kwargs: Sessions)
    monkeypatch.setattr(knowledge_api, "tool_execution_slot", slot)
    monkeypatch.setattr(knowledge_api, "search_knowledge", slow_search)
    monkeypatch.setattr(knowledge_tool, "search_knowledge", slow_search)

    async def exercise() -> None:
        """真实路由函数返回 504，Agent 工具传播超时交给统一运行事件处理。"""
        with pytest.raises(CustomException) as failure:
            await knowledge_api.search_knowledge_base(1, KnowledgeSearchIn(query="审查要求"), actor=SimpleNamespace(id=1), db=SimpleNamespace(bind=None))
        assert failure.value.status_code == 504
        assert leases == [0.03, "released"]
        context = SimpleNamespace(
            sessions=Sessions,
            run_id="run-id",
            owner_id=1,
            require_active=AsyncMock(return_value=SimpleNamespace(tool_timeout_seconds=1)),
        )
        search = knowledge_tool.knowledge_search_tool(context)
        with pytest.raises(TimeoutError):
            await search.ainvoke(
                {
                    "query": "审查要求",
                },
            )
        assert stopped == [True, True]

    asyncio.run(exercise())


def test_embedding_request_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """检索与索引共用的嵌入请求采用配置时限，即使客户端由调用方注入也不会绕过。"""
    monkeypatch.setattr(get_config(), "KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS", 0.03)
    model = ModelCatalog(id=1, external_id="embedding", provider_id=1)
    provider = ProviderConnection(id=1, kind=ProviderKind.OLLAMA, base_url="http://embedding.test/v1", timeout_seconds=1)
    stopped = []

    async def slow_response(request):
        """用真实 HTTPX 调用路径模拟慢响应，不访问外部服务。"""
        try:
            await asyncio.sleep(1)
        finally:
            stopped.append(True)

    async def exercise() -> None:
        """确认超时被归为脱敏的知识处理超时，并取消底层请求。"""
        async with httpx.AsyncClient(transport=httpx.MockTransport(slow_response)) as client:
            with pytest.raises(KnowledgeProcessingError) as failure:
                await embed_texts(model, provider, ["审查查询"], dimensions=2, client=client)
            assert failure.value.code == KnowledgeFailure.TIMEOUT
        assert stopped == [True]

    asyncio.run(exercise())
