# -*- coding: utf-8 -*-
# @Time    : 2026-11-23
# @Author  : Codex
# @File    : test_mcp_tool_search.py

"""通过正式图、检查点与 Worker 验证固定授权集合内的 MCP 按需加载。"""

import asyncio
import json

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import Field
import pytest
from sqlalchemy import select

from app.core.custom_exception import CustomException
from app.core.config import get_config
from app.models.agent_run import AgentRun
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services.mcp_tool_search import search_score, selected_mcp_tools
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run, decide_approval
from app.services.worker_tools import make_worker_tool_resolver
from app.services import mcp_tools
from tests.test_mcp_results import remote_server
from tests.test_mcp_run_sessions import database


class SearchModel(FakeMessagesListChatModel):
    """只替换上游模型，记录每次实际绑定的普通工具 Schema 与收到的结果。"""

    bindings: list[set[str]] = Field(default_factory=list)
    received: list[list] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        """不提供任何供应商专用搜索能力。"""
        self.bindings.append({tool.name for tool in tools})
        return self

    async def _agenerate(self, messages, *args, **kwargs):
        """记录经摘要和授权后的真实模型输入。"""
        self.received.append(messages)
        return await super()._agenerate(messages, *args, **kwargs)


def call(name, arguments=None, identity=None):
    """生成模型工具调用，不伪造工具结果或检查点状态。"""
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": arguments or {},
                "id": identity or name,
            }
        ],
    )


async def fixed_tools(db, run, *, count=17, write=False, huge=False, url="http://localhost:9999/mcp"):
    """发布并固定版本；另建未授权私有工具，证明搜索不会扫描全站。"""
    for index in range(1, count + 1):
        db.add(
            UserTool(
                id=index,
                owner_id=1,
                name=f"query_{index}",
                display_name=f"订单查询 {index}",
                description=f"读取订单 {index}",
                kind="mcp",
                visibility="private",
                current_version=1,
            )
        )
        db.add(
            UserToolVersion(
                tool_id=index,
                version=1,
                content_hash=f"{index:064x}",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "description": "仅用于预算的大型参数说明" * (1500 if huge else 1),
                },
                effect="write" if write else "read",
                recovery_policy="manual" if write else "safe",
                requires_approval=write,
                verified=True,
                mcp_url=url,
                mcp_tool_name="create_orders" if write else f"query_{index}",
            )
        )
        db.add(RunTool(run_id=run.id, tool_id=index, tool_version=1, content_hash=f"{index:064x}", requires_approval=write))
    db.add(UserTool(id=99, owner_id=2, name="private_other", description="private-marker", kind="mcp", visibility="private"))


def test_dynamic_graph_loads_only_matching_schemas_and_replaces_group(tmp_path, monkeypatch):
    """17 个大 Schema 不预先灌入；名称精确检索、换组与未加载调用均走真实图。"""

    async def exercise():
        monkeypatch.setattr(get_config(), "MCP_TOOL_SCHEMA_MODE", "dynamic")
        async with database(tmp_path / "search.db") as sessions:
            async with sessions.begin() as db:
                db.add(ProviderConnection(id=1, name="search", name_key="search", kind="ollama", base_url="http://localhost:11434", creator_id=1))
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="search",
                        display_name="search",
                        context_window=32768,
                        verified_capabilities=["text", "tools"],
                        creator_id=1,
                    )
                )
                run = await create_run(db, 1, model_id=1, input_text="查两张订单", agent_max_output_tokens=512)
                await fixed_tools(db, run, huge=True)
            executed = []

            async def resolve(db, current):
                tools = []
                for index in range(1, 18):

                    async def read(_index=index):
                        """记录是否真的调用了业务实现。"""
                        executed.append(_index)
                        return "查到订单"

                    tools.append(
                        StructuredTool.from_function(
                            coroutine=read,
                            name=f"user_tool_{index}_query_{index}",
                            description="订单查询",
                            args_schema={
                                "type": "object",
                                "properties": {
                                    "value": {
                                        "type": "string",
                                        "description": "大型中文参数说明" * 1500,
                                    },
                                },
                            },
                        )
                    )
                return tools, {}

            model = SearchModel(
                responses=[
                    call("user_tool_1_query_1", identity="guess"),
                    call(
                        "tool_search",
                        {
                            "query": "user_tool_1_query_1",
                            "limit": 1,
                        },
                        "find-1",
                    ),
                    call("user_tool_1_query_1", identity="read-1"),
                    call(
                        "tool_search",
                        {
                            "query": "user_tool_2_query_2",
                            "limit": 1,
                        },
                        "find-2",
                    ),
                    call("user_tool_2_query_2", identity="read-2"),
                    AIMessage(content="两张订单已查询"),
                ]
            )
            result = await execute_text_run(sessions, run.id, model, tmp_path / "checkpoint.db", resolve_tools=resolve)
            assert result.state == "completed", result.failure_code
            assert executed == [1, 2]
            assert model.bindings[0] == {"tool_search"}
            assert any(names == {"tool_search", "user_tool_1_query_1"} for names in model.bindings)
            assert model.bindings[-1] == {"tool_search", "user_tool_2_query_2"}
            all_results = [message for message in model.received[-1] if isinstance(message, ToolMessage)]
            assert any(message.status == "error" and "尚未加载" in message.content for message in all_results)
            assert "private-marker" not in str(all_results)
            async with sessions() as db:
                records = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id))).all()
                budget = (await db.get(AgentRun, run.id)).context_budget
            assert len(records) == 2 and all(item.state == "completed" for item in records)
            assert budget["fits"] is True
            assert budget["tool_loading"] == {
                "mode": "dynamic",
                "available_mcp_tools": 17,
                "loaded_mcp_tools": 1,
            }

    asyncio.run(exercise())


@pytest.mark.parametrize("decision, mode", [("approve", "stored"), ("reject_continue", "stored"), ("approve", "remote_error"), ("approve", "transport_limit")])
def test_dynamic_real_http_write_survives_new_worker_and_approval(tmp_path, monkeypatch, decision, mode):
    """实际 MCP TCP 写调用；搜索不执行，跨运行时恢复审批，拒绝后搜索也不能重新找到。"""

    async def exercise():
        monkeypatch.setattr(get_config(), "MCP_TOOL_SCHEMA_MODE", "dynamic")
        monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "artifacts"))
        async with database(tmp_path / "write.db") as sessions, remote_server(monkeypatch, error=mode == "remote_error") as (url, calls):
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="只创建一次订单")
                await fixed_tools(db, run, count=1, write=True, url=url)
                await db.flush()
                version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == 1))
                version.input_schema = (await mcp_tools.discover_mcp_tools(url))[0]["input_schema"]
            if mode == "transport_limit":
                monkeypatch.setattr(get_config(), "MCP_RESPONSE_MAX_BYTES", 8192)
            model = SearchModel(
                responses=[
                    call(
                        "tool_search",
                        {
                            "query": "create_orders",
                        },
                        "find",
                    ),
                    call("user_tool_1_query_1", identity="create-once"),
                    call(
                        "tool_search",
                        {
                            "query": "create_orders",
                        },
                        "find-after-approval",
                    ),
                    AIMessage(content="操作已处理，不重做"),
                ]
            )

            async def resolve_model(db, current):
                return model

            checkpoint = tmp_path / "checkpoint.db"
            paused = await dispatch_next_run(sessions, checkpoint, resolve_model=resolve_model, resolve_tools=make_worker_tool_resolver(sessions))
            assert paused.state == "waiting_for_approval", paused.failure_code
            assert not calls
            async with sessions.begin() as db:
                await decide_approval(db, run.id, 1, decision)
            # 新建 resolver 与运行时，选择必须来自检查点，不能依赖旧对象内存。
            result = await dispatch_next_run(sessions, checkpoint, resolve_model=resolve_model, resolve_tools=make_worker_tool_resolver(sessions))
            if mode in {"remote_error", "transport_limit"}:
                assert result.state == "failed"
                assert calls == [1]
                async with sessions() as db:
                    invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
                assert invocation.state == "unknown"
                return
            assert result.state == "completed", result.failure_code
            assert calls == ([1] if decision == "approve" else [])
            last_search = next(message for message in reversed(model.received[-1]) if isinstance(message, ToolMessage) and message.name == "tool_search")
            payload = json.loads(last_search.content)
            assert payload["total_matches"] == (1 if decision == "approve" else 0)
            async with sessions() as db:
                invocations = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id))).all()
            assert len(invocations) == (1 if decision == "approve" else 0)

    asyncio.run(exercise())


def test_selection_is_scoped_to_run_and_search_handles_chinese():
    """检查点跨轮不得继承加载；中文用途、大小写名称和空白查询有确定含义。"""
    assert (
        selected_mcp_tools(
            {
                "mcp_tool_selection": {
                    "run_id": "old",
                    "names": ["tool"],
                },
            },
            "new",
        )
        == frozenset()
    )
    assert selected_mcp_tools(
        {
            "mcp_tool_selection": {
                "run_id": "new",
                "names": ["tool"],
            },
        },
        "new",
    ) == {"tool"}
    assert search_score("查询订单状态", ["search", "订单查询", "订单状态检索"]) > 0
    assert search_score("SEARCH", ["search"]) > search_score("search", ["other", "用于 search"])
    assert search_score("   ", ["search"]) == 0


def test_search_paging_duplicate_names_fixed_version_and_revocation(tmp_path, monkeypatch):
    """同名工具按 ID 区分，只返回 Run v1；分页不泄露未授权工具，撤权后拒绝搜索。"""
    from langchain.tools import ToolRuntime
    from app.services.mcp_tool_search import make_tool_search
    from app.services.tool_registry import ToolContext
    from tests.test_mcp_run_sessions import running

    async def exercise():
        async with database(tmp_path / "pages.db") as sessions:
            run = await running(sessions)
            async with sessions.begin() as db:
                await fixed_tools(db, run, count=2)
                await db.flush()
                for index in (1, 2):
                    definition = await db.get(UserTool, index)
                    definition.name = "search"
                    definition.owner_id = index
                    definition.visibility = "public"
                    definition.current_version = 2
                    if index == 2:
                        binding = await db.scalar(select(RunTool).where(RunTool.tool_id == index))
                        binding.requires_approval = True
                    version = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == index))
                    version.mcp_tool_name = "search"
                    db.add(
                        UserToolVersion(
                            tool_id=index,
                            version=2,
                            content_hash="b" * 64,
                            input_schema={
                                "type": "object",
                                "properties": {},
                            },
                            effect="read",
                            recovery_policy="safe",
                            requires_approval=False,
                            verified=True,
                            mcp_tool_name="search-v2",
                        )
                    )

            async def read():
                return "此测试不应执行远端操作"

            tools = {
                f"user_tool_{index}_search": StructuredTool.from_function(coroutine=read, name=f"user_tool_{index}_search", description="订单搜索")
                for index in (1, 2)
            }
            search = make_tool_search(ToolContext(sessions, run.id, 1, run.lease_token), tools)
            runtime = ToolRuntime(state={}, context=None, config={}, stream_writer=lambda message: None, tool_call_id="search-pages", store=None)
            assert "runtime" not in search.args
            pages = []
            for offset in (0, 1):
                result = await search.coroutine("search", runtime, limit=1, offset=offset)
                payload = json.loads(result.update["messages"][0].content)
                assert payload["total_matches"] == 2
                assert payload["results"][0]["tool_version"] == 1
                assert payload["results"][0]["remote_name"] == "search"
                assert payload["next_offset"] == (1 if offset == 0 else None)
                pages.append(payload["loaded_names"])
            assert pages == [["user_tool_1_search"], ["user_tool_2_search"]]
            missing = await search.coroutine("private-marker", runtime)
            assert not missing.update["mcp_tool_selection"]["names"]
            async with sessions.begin() as db:
                (await db.get(UserTool, 2)).enabled = False
            with pytest.raises(CustomException, match="Tool 授权或版本已失效|Tool 审批策略"):
                await search.coroutine("*", runtime)
            async with sessions.begin() as db:
                (await db.get(UserTool, 2)).enabled = True
            from app.services import mcp_tool_search

            original_load = mcp_tool_search.load_run_tools
            reads = []

            async def revoke_during_search(db, run_id, owner_id):
                """第一次校验后撤权，第二次必须使用新会话读到最新状态。"""
                reads.append(1)
                result = await original_load(db, run_id, owner_id)
                if len(reads) == 1:
                    async with sessions.begin() as writer:
                        (await writer.get(UserTool, 2)).enabled = False
                return result

            monkeypatch.setattr(mcp_tool_search, "load_run_tools", revoke_during_search)
            with pytest.raises(CustomException, match="Tool 授权或版本已失效"):
                await search.coroutine("*", runtime)
            assert len(reads) == 2

    asyncio.run(exercise())


def test_dynamic_single_loaded_schema_still_obeys_context_budget(tmp_path, monkeypatch):
    """动态模式不能规避最终完整请求预算；单个巨型工具也会在绑定后的模型请求前拒绝。"""

    async def exercise():
        monkeypatch.setattr(get_config(), "MCP_TOOL_SCHEMA_MODE", "dynamic")
        async with database(tmp_path / "huge.db") as sessions:
            async with sessions.begin() as db:
                db.add(ProviderConnection(id=1, name="huge", name_key="huge", kind="ollama", base_url="http://localhost:11434", creator_id=1))
                db.add(
                    ModelCatalog(
                        id=1, provider_id=1, external_id="huge", display_name="huge", context_window=8192, verified_capabilities=["text", "tools"], creator_id=1
                    )
                )
                run = await create_run(db, 1, model_id=1, input_text="查询订单", agent_max_output_tokens=512)
                await fixed_tools(db, run, count=1)

            async def read():
                raise AssertionError("预算不足，不应执行业务工具")

            async def resolve(db, current):
                return [
                    StructuredTool.from_function(
                        coroutine=read,
                        name="user_tool_1_query_1",
                        description="订单查询",
                        args_schema={
                            "type": "object",
                            "properties": {
                                "value": {
                                    "type": "string",
                                    "description": "巨型中文 Schema" * 12000,
                                },
                            },
                        },
                    )
                ], {}

            model = SearchModel(
                responses=[
                    call(
                        "tool_search",
                        {
                            "query": "query_1",
                        },
                    ),
                    AIMessage(content="不应到达模型"),
                ]
            )
            result = await execute_text_run(sessions, run.id, model, tmp_path / "checkpoint.db", resolve_tools=resolve)
            assert result.state == "failed" and result.failure_code == "context_window_exceeded"
            assert len(model.received) == 1
            async with sessions() as db:
                budget = (await db.get(AgentRun, run.id)).context_budget
            assert budget["fits"] is False and budget["components"]["tools"]["tokens"] > 8192

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "ollama", "anthropic", "azure_openai", "gemini", "bedrock"])
def test_search_uses_existing_provider_tool_schema_adapters(monkeypatch, kind):
    """实际 SDK 仅构造并序列化普通函数声明，不发送付费或真实云请求。"""
    from app.services.model_adapter import build_chat_model
    from app.services.mcp_tool_search import make_tool_search
    from app.services.provider_credentials import encrypt_credentials
    from app.services.tool_registry import ToolContext

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "fixture-only")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fixture-only")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    provider = ProviderConnection(
        id=1,
        kind=kind,
        base_url="https://example.invalid",
        api_version="2024-10-21" if kind == "azure_openai" else None,
        region="us-east-1",
        encrypted_credentials=encrypt_credentials("fixture-only", {}),
        timeout_seconds=5,
        status=1,
        is_deleted=0,
    )
    model = ModelCatalog(
        id=1,
        provider_id=1,
        external_id="anthropic.claude-3-sonnet-20240229-v1:0" if kind == "bedrock" else "fixture-model",
        status=1,
        is_deleted=0,
    )
    runtime = build_chat_model(model, provider)
    # 构造函数不会读取会话；此专项只验证适配器绑定，权限行为由正式图专项覆盖。
    search = make_tool_search(ToolContext(None, "schema-only", 1, "unused"), {})
    bound = runtime.bind_tools([search])
    serialized = json.dumps(bound.kwargs, ensure_ascii=False, default=str)
    assert "tool_search" in serialized and "query" in serialized and "offset" in serialized
    assert "tool_call_id" not in serialized and "fixture-only" not in serialized
