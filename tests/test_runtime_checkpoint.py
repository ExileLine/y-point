# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 21:43:29
# @Author  : yangyuexiong
# @File    : test_runtime_checkpoint.py

"""验证 DeepAgents 检查点可在独立进程之间恢复。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.model_adapter import build_chat_model
from app.services.runtime_checkpoint import (
    open_async_checkpointer,
    open_async_sqlite_checkpointer,
    open_sqlite_checkpointer,
)
from app.services import runtime_checkpoint


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """让假模型接受工具绑定并保留预置回答。"""
        return self


@tool
def echo(text: str) -> str:
    """返回输入文本，供审批恢复验证。"""
    return text


def run_phase(phase: str, database_path: Path) -> dict:
    """在一个进程内运行指定阶段并返回不含对话正文的验证结果。"""
    responses = {
        "start": AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "hello"}, "id": "call-1"}]),
        "resume": AIMessage(content="finished"),
        "followup": AIMessage(content="second answer"),
        "read": AIMessage(content="unused"),
    }
    model = ScriptedModel(responses=[responses[phase]])
    config = {"configurable": {"thread_id": "persistent-approval"}}
    with open_sqlite_checkpointer(database_path) as checkpointer:
        agent = create_deep_agent(model=model, tools=[echo], interrupt_on={"echo": True}, checkpointer=checkpointer)
        if phase == "start":
            result = agent.invoke({"messages": [{"role": "user", "content": "say hello"}]}, config)
            assert result["__interrupt__"]
        elif phase == "resume":
            result = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            assert not result.get("__interrupt__")
        elif phase == "followup":
            events = list(
                agent.stream({"messages": [{"role": "user", "content": "one more"}]}, config, stream_mode=["updates", "messages"])
            )
            assert any(mode == "messages" for mode, _ in events)
        state = agent.get_state(config)
        messages = state.values["messages"]
        return {
            "checkpoint_id": state.config["configurable"]["checkpoint_id"],
            "has_tool_result": any(isinstance(message, ToolMessage) and message.content == "hello" for message in messages),
            "last_message": messages[-1].content,
            "message_count": len(messages),
            "pending": bool(state.next),
        }


def worker(phase: str, database_path: Path) -> dict:
    """在全新 Python 进程中执行一个检查点阶段。"""
    result = subprocess.run(
        [sys.executable, "-m", "tests.test_runtime_checkpoint", phase, str(database_path)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_sqlite_checkpoint_recovers_approval_and_next_turn(tmp_path: Path) -> None:
    """验证审批中断、跨进程恢复和后续流式对话均使用同一持久化线程。"""
    database_path = tmp_path / "runtime-checkpoints.db"
    started = worker("start", database_path)
    assert started["pending"] is True
    assert started["has_tool_result"] is False

    resumed = worker("resume", database_path)
    assert resumed["pending"] is False
    assert resumed["has_tool_result"] is True
    assert resumed["last_message"] == "finished"
    assert resumed["checkpoint_id"] != started["checkpoint_id"]

    followed_up = worker("followup", database_path)
    assert followed_up["last_message"] == "second answer"
    assert followed_up["message_count"] > resumed["message_count"]
    assert worker("read", database_path) == followed_up


def test_sqlite_checkpoint_requires_file() -> None:
    """拒绝内存数据库，以免错误地声称运行状态可在重启后恢复。"""
    with pytest.raises(ValueError, match="文件路径"), open_sqlite_checkpointer(Path(":memory:")):
        pass


def test_async_sqlite_checkpoint_recovers_approval(tmp_path: Path) -> None:
    """验证异步运行时在检查点连接重开后恢复待审批工具。"""

    async def exercise() -> None:
        """在两次独立的异步检查点连接中完成审批恢复。"""
        database_path = tmp_path / "async-checkpoints.db"
        config = {"configurable": {"thread_id": "async-approval"}}
        tool_call = AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "hello"}, "id": "call-async"}])
        async with open_async_sqlite_checkpointer(database_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[tool_call]), tools=[echo], interrupt_on={"echo": True}, checkpointer=checkpointer,
            )
            paused = await agent.ainvoke({"messages": [{"role": "user", "content": "say hello"}]}, config)
            assert paused["__interrupt__"]
        async with open_async_sqlite_checkpointer(database_path) as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="finished")]), tools=[echo],
                interrupt_on={"echo": True}, checkpointer=checkpointer,
            )
            resumed = await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            assert any(isinstance(message, ToolMessage) and message.content == "hello" for message in resumed["messages"])
            assert resumed["messages"][-1].content == "finished"

    asyncio.run(exercise())


def test_postgres_checkpoint_requires_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置错误必须在 worker 领取运行前暴露。"""
    monkeypatch.setattr(runtime_checkpoint, "get_config", lambda: SimpleNamespace(
        CHECKPOINT_BACKEND="postgres", CHECKPOINT_POSTGRES_DSN="",
    ))

    async def exercise() -> None:
        with pytest.raises(ValueError, match="独立连接地址"):
            async with open_async_checkpointer(tmp_path / "unused.sqlite"):
                pass

    asyncio.run(exercise())


@pytest.mark.skipif(
    not os.environ.get("YPOINT_TEST_POSTGRES_CHECKPOINT_DSN"), reason="需显式提供隔离 PostgreSQL 检查点库",
)
def test_postgres_checkpoint_recovers_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """真实 PostgreSQL 检查点在断开连接后恢复同一审批和工具结果。"""
    monkeypatch.setattr(runtime_checkpoint, "get_config", lambda: SimpleNamespace(
        CHECKPOINT_BACKEND="postgres", CHECKPOINT_POSTGRES_DSN=os.environ["YPOINT_TEST_POSTGRES_CHECKPOINT_DSN"],
    ))

    async def exercise() -> None:
        config = {"configurable": {"thread_id": f"checkpoint-{uuid4()}"}}
        tool_call = AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "hello"}, "id": "call-postgres"}])
        async with open_async_checkpointer(tmp_path / "unused.sqlite") as checkpointer:
            await checkpointer.setup()
            agent = create_deep_agent(
                model=ScriptedModel(responses=[tool_call]), tools=[echo], interrupt_on={"echo": True}, checkpointer=checkpointer,
            )
            paused = await agent.ainvoke({"messages": [{"role": "user", "content": "say hello"}]}, config)
            assert paused["__interrupt__"]
            paused_id = (await agent.aget_state(config)).config["configurable"]["checkpoint_id"]
        async with open_async_checkpointer(tmp_path / "unused.sqlite") as checkpointer:
            agent = create_deep_agent(
                model=ScriptedModel(responses=[AIMessage(content="finished")]), tools=[echo],
                interrupt_on={"echo": True}, checkpointer=checkpointer,
            )
            resumed = await agent.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
            assert any(isinstance(message, ToolMessage) and message.content == "hello" for message in resumed["messages"])
            assert resumed["messages"][-1].content == "finished"
            assert (await agent.aget_state(config)).config["configurable"]["checkpoint_id"] != paused_id

    asyncio.run(exercise())


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_OLLAMA_M3") != "1", reason="仅在显式启用本机 Ollama 集成验收时运行")
def test_live_ollama_deep_agent_tool_stream_and_usage() -> None:
    """使用本机真实模型验证 DeepAgents 工具编排、流式事件和 Token 用量。"""
    provider = ProviderConnection(
        id=999003, kind="ollama", base_url="http://127.0.0.1:11434/v1", encrypted_credentials=None,
        timeout_seconds=60, status=1, is_deleted=0,
    )
    model = ModelCatalog(id=999003, provider_id=provider.id, external_id="gemma4:31b-cloud", status=1, is_deleted=0)
    agent = create_deep_agent(
        model=build_chat_model(model, provider), tools=[echo], checkpointer=InMemorySaver(),
        system_prompt="Call the echo tool exactly once with text 'hello'. After the tool result, give a short final answer.",
    )
    config = {"configurable": {"thread_id": "live-ollama-m3"}}
    events = list(agent.stream({"messages": [{"role": "user", "content": "Use echo now."}]}, config, stream_mode=["updates", "messages"]))
    messages = agent.get_state(config).values["messages"]

    assert any(mode == "messages" for mode, _ in events)
    assert any(mode == "updates" and "tools" in payload for mode, payload in events)
    assert any(isinstance(message, ToolMessage) and message.content == "hello" for message in messages)
    assert any(isinstance(message, AIMessage) and message.usage_metadata for message in messages)
    assert isinstance(messages[-1], AIMessage) and bool(messages[-1].content)


if __name__ == "__main__":
    print(json.dumps(run_phase(sys.argv[1], Path(sys.argv[2]))))
