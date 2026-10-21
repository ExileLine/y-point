"""Executable API checks for the pinned DeepAgents release (no provider key needed)."""

import asyncio

from deepagents import create_deep_agent
from deepagents.backends.utils import create_file_data
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self


@tool
def echo(text: str) -> str:
    """Return the provided text."""
    return text


def tool_model() -> ScriptedModel:
    return ScriptedModel(
        responses=[
            AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "hello"}, "id": "call-1"}]),
            AIMessage(content="finished"),
        ]
    )


def test_tool_stream_and_checkpoint() -> None:
    checkpointer = InMemorySaver()
    agent = create_deep_agent(model=tool_model(), tools=[echo], checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "tool-stream"}}

    events = list(agent.stream({"messages": [{"role": "user", "content": "say hello"}]}, config, stream_mode=["updates", "messages"]))
    state = agent.get_state(config)

    assert any(mode == "messages" for mode, _ in events)
    assert any(mode == "updates" and "tools" in payload for mode, payload in events)
    assert any(isinstance(message, ToolMessage) and message.content == "hello" for message in state.values["messages"])
    assert state.values["messages"][-1].content == "finished"
    assert state.config["configurable"]["checkpoint_id"]


def test_skill_loading() -> None:
    agent = create_deep_agent(
        model=ScriptedModel(responses=[AIMessage(content="ok")]),
        skills=["/skills/"],
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "skill"}}
    skill = "---\nname: example\ndescription: A local verification skill\n---\n\n# Example\nUse concise answers.\n"

    agent.invoke({
        "messages": [{"role": "user", "content": "hello"}],
        "files": {"/skills/example/SKILL.md": create_file_data(skill)},
    }, config)

    assert any(item["name"] == "example" for item in agent.get_state(config).values["skills_metadata"])


def test_approval_resume() -> None:
    agent = create_deep_agent(model=tool_model(), tools=[echo], interrupt_on={"echo": True}, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "approval"}}

    paused = agent.invoke({"messages": [{"role": "user", "content": "say hello"}]}, config)
    assert paused["__interrupt__"]
    assert not any(isinstance(message, ToolMessage) for message in paused["messages"])

    resumed = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    assert any(isinstance(message, ToolMessage) and message.content == "hello" for message in resumed["messages"])
    assert resumed["messages"][-1].content == "finished"


def test_async_cancellation_reaches_tool() -> None:
    async def check() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()

        @tool
        async def slow_tool() -> str:
            """Wait until cancelled."""
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                stopped.set()
            return "unexpected"

        model = ScriptedModel(responses=[
            AIMessage(content="", tool_calls=[{"name": "slow_tool", "args": {}, "id": "call-slow"}]),
            AIMessage(content="finished"),
        ])
        agent = create_deep_agent(model=model, tools=[slow_tool], checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": "cancel"}}

        async def consume() -> None:
            async for _ in agent.astream({"messages": [{"role": "user", "content": "wait"}]}, config):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Agent did not propagate cancellation")
        assert stopped.is_set()

    asyncio.run(check())
