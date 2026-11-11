# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 15:19:30
# @Author  : yangyuexiong
# @File    : test_conversation_history.py

"""验证摘要历史的超长单行分页、读取预算和可恢复性。"""

import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock

from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import ToolMessage
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.base import Base
from app.services.run_executor import AuthorizedToolsMiddleware
from app.services.run_manager import claim_run, create_run


def test_history_long_line_is_paged_without_losing_source(tmp_path: Path) -> None:
    """复现一行五万字符的历史正文，要求每次读取受限并可通过后续 offset 取回尾部事实。"""

    async def exercise() -> None:
        """走正式授权、预算和事件链路，检查首段不能重新塞入整份原文。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'history-pages.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        source = "开头事实。" + "施工方案正文，设计参数与核算依据。" * 3000 + "尾部事实：锚固长度12米。"
        path = "/conversation_history/session_current.md"
        state = {
            "files": {
                path: {
                    "content": source,
                },
            },
        }
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="history-pages-owner", password="unused", role="user"))
                run = await create_run(db, 1, max_tool_calls=30)
                run = await claim_run(db, run.id)
            middleware = AuthorizedToolsMiddleware(set(), sessions, run.id, run.lease_token, 30, None, None, None, None)
            offset = 0
            recovered = []
            handler = AsyncMock(return_value=ToolMessage(content=source, tool_call_id="history-page", name="read_file"))
            for page in range(20):
                result = await middleware._execute_authorized_tool_call(
                    ToolCallRequest(
                        tool_call={
                            "name": "read_file",
                            "args": {
                                "file_path": path,
                                "offset": offset,
                                "limit": 500,
                            },
                            "id": "history-page",
                        },
                        tool=None,
                        state=state,
                        runtime=None,
                    ),
                    handler,
                )
                assert isinstance(result, ToolMessage) and result.status == "success"
                assert len(result.content) <= 8192, "按行分页不能把超长单行原文完整塞回上下文"
                recovered.extend(re.findall(r"^\s*\d+\t(.*)$", result.content, re.MULTILINE))
                match = re.search(r"next_offset=(\d+)", result.content)
                if match is None:
                    assert "EOF" in result.content
                    break
                assert int(match.group(1)) > offset
                offset = int(match.group(1))
            else:
                raise AssertionError("分页必须能到达原文末尾")
            assert "".join(recovered) == source
            assert page > 0
            handler.assert_not_called()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_history_snapshot_does_not_chase_appended_archive() -> None:
    """摘要继续往同一历史文件追加内容时，本轮分页仍能走到首次读取快照的末尾。"""
    from app.services.conversation_history import read_conversation_history

    path = "/conversation_history/current.md"
    state = {
        "files": {
            path: {
                "content": "原文。" * 6000,
            },
        },
    }
    snapshots = {}
    offset = 0
    recovered = []
    for page in range(10):
        result = read_conversation_history(
            state,
            {
                "args": {
                    "file_path": path,
                    "offset": offset,
                    "limit": 2000,
                },
                "id": "stable-history-page",
            },
            snapshots,
        )
        recovered.extend(re.findall(r"^\s*\d+\t(.*)$", result.content, re.MULTILINE))
        state["files"][path]["content"] += "新追加的读取记录。" * 2000
        match = re.search(r"next_offset=(\d+)", result.content)
        if match is None:
            assert "EOF" in result.content
            break
        offset = int(match.group(1))
    else:
        raise AssertionError("快照分页不能追逐摘要追加的内容而永远读不到 EOF")
    assert page > 0
    assert "".join(recovered) == "原文。" * 6000


def test_history_legacy_lines_and_invalid_paging_arguments() -> None:
    """兼容旧式行数组，拒绝越权路径和无效分页参数，超出末尾返回 EOF。"""
    from app.services.conversation_history import read_conversation_history

    path = "/conversation_history/current.md"
    state = {
        "files": {
            path: {
                "content": ["首行", "", "末行"],
            },
        },
    }
    arguments = {
        "file_path": path,
        "offset": 0,
        "limit": 3,
    }
    result = read_conversation_history(
        state,
        {
            "args": arguments,
            "id": "legacy-history",
        },
        {},
    )
    assert result.status == "success" and "EOF" in result.content
    assert re.findall(r"^\s*\d+\t(.*)$", result.content, re.MULTILINE) == ["首行", "", "末行"]
    invalid = [
        {
            "file_path": "/etc/passwd",
        },
        {
            "file_path": "/conversation_history/../current.md",
        },
        {
            "offset": -1,
        },
        {
            "offset": True,
        },
        {
            "offset": "0",
        },
        {
            "limit": 0,
        },
        {
            "limit": False,
        },
        {
            "limit": "3",
        },
    ]
    for override in invalid:
        result = read_conversation_history(
            state,
            {
                "args": {
                    **arguments,
                    **override,
                },
                "id": "invalid-history",
            },
            {},
        )
        assert result.status == "error"
    result = read_conversation_history(
        state,
        {
            "args": {
                **arguments,
                "offset": 100,
            },
            "id": "eof-history",
        },
        {},
    )
    assert result.status == "success" and "EOF" in result.content
    assert "末行" not in result.content
