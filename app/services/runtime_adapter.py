# -*- coding: utf-8 -*-
# @Time    : 2026-10-25 13:30:00
# @Author  : yangyuexiong
# @File    : runtime_adapter.py

"""将平台 Run 配置映射到 DeepAgents 的文本运行接口。"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from deepagents import create_deep_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.types import Command

from app.models.agent_run import AgentRun


@dataclass(frozen=True)
class RuntimeRunConfig:
    """运行领取后固定的编排配置；权限仍在工具调用时重新检查。"""

    run_id: str
    owner_id: int
    thread_id: str
    model_id: int | None
    tool_names: tuple[str, ...]
    system_prompt: str | None = None

    @classmethod
    def from_run(cls, run: AgentRun, tools: Sequence[BaseTool]) -> "RuntimeRunConfig":
        return cls(run.id, run.owner_id, run.thread_id, run.model_id, tuple(tool.name for tool in tools), run.agent_system_prompt)

    def graph_config(self) -> dict:
        return {
            "configurable": {"thread_id": self.thread_id},
            "metadata": {"run_id": self.run_id, "tool_count": len(self.tool_names)},
        }


class RuntimeAdapter(Protocol):
    async def state(self) -> Any: ...

    def events(self, invocation: dict | Command) -> AsyncIterator[tuple[str, Any]]: ...


class DeepAgentsRuntimeAdapter:
    """只负责图的创建、状态读取及流消费；事务和授权由平台执行器负责。"""

    def __init__(
        self, config: RuntimeRunConfig, *, model: BaseChatModel, tools: list[BaseTool],
        interrupt_on: dict[str, bool], checkpointer: Any, middleware: list[AgentMiddleware],
    ) -> None:
        self.config = config.graph_config()
        system_prompt = config.system_prompt or ""
        if "create_text_artifact" in config.tool_names:
            system_prompt += (
                "\n当用户要求生成可下载的文件时，按扩展名调用对应工具："
                "TXT/Markdown/HTML/JSON/CSV 使用 create_text_artifact，DOCX/PDF 使用 create_document_artifact，"
                "XLSX 使用 create_spreadsheet_artifact。工具成功后再回复用户。"
                "Markdown 代码块不能代替文件产物。"
            )
        self.agent = create_deep_agent(
            model=model, tools=tools, interrupt_on=interrupt_on,
            checkpointer=checkpointer, middleware=middleware, system_prompt=system_prompt or None,
        )

    async def state(self) -> Any:
        return await self.agent.aget_state(self.config)

    async def events(self, invocation: dict | Command) -> AsyncIterator[tuple[str, Any]]:
        async for event in self.agent.astream(invocation, self.config, stream_mode=["messages", "updates"]):
            yield event
