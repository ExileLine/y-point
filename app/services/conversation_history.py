# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 15:21:36
# @Author  : yangyuexiong
# @File    : conversation_history.py

"""当前线程摘要历史的授权路径与有界分页视图。"""

from pathlib import PurePosixPath

from langchain_core.messages import ToolMessage

HISTORY_PAGE_MAX_CHARS = 8192
HISTORY_VIEW_LINE_CHARS = 512


def conversation_history_paths(state: dict) -> frozenset[str]:
    """只返回当前线程直接位于历史目录、实际存在且路径规范的 Markdown 文件。"""
    files = state.get("files")
    if not isinstance(files, dict):
        return frozenset()
    paths = frozenset(
        path
        for path in files
        if isinstance(path, str)
        and PurePosixPath(path).parent == PurePosixPath("/conversation_history")
        and PurePosixPath(path).suffix == ".md"
        and str(PurePosixPath(path)) == path
    )
    return paths


def read_conversation_history(state: dict, tool_call: dict, snapshots: dict[str, list[str]]) -> ToolMessage:
    """以稳定的软换行视图分页，限制整个响应长度，并明确返回后续 offset，不改写原始历史。"""
    arguments = tool_call.get("args", {})
    path = arguments.get("file_path")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", 2000)
    if (
        not isinstance(path, str)
        or path not in conversation_history_paths(state)
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit <= 0
    ):
        return ToolMessage(
            content="历史文件未授权或分页参数无效；offset 必须为非负整数，limit 必须为正整数。",
            tool_call_id=tool_call["id"],
            name="read_file",
            status="error",
        )
    data = state["files"][path]
    source_lines = data.get("content") if isinstance(data, dict) else None
    if isinstance(source_lines, str):
        source_lines = source_lines.split("\n")
    if not isinstance(source_lines, list) or any(not isinstance(line, str) for line in source_lines):
        return ToolMessage(content="当前历史文件内容不可读。", tool_call_id=tool_call["id"], name="read_file", status="error")
    if path not in snapshots:
        snapshot = []
        for line in source_lines:
            snapshot.extend(line[index : index + HISTORY_VIEW_LINE_CHARS] for index in range(0, len(line), HISTORY_VIEW_LINE_CHARS))
            if not line:
                snapshot.append("")
        snapshots[path] = snapshot
    lines = snapshots[path]
    end = min(len(lines), offset + limit)
    rows = []
    size = 0
    next_offset = min(offset, len(lines))
    for index in range(offset, end):
        row = f"{index + 1:6}\t{lines[index]}"
        if size + len(row) + 1 > HISTORY_PAGE_MAX_CHARS - 512:
            break
        rows.append(row)
        size += len(row) + 1
        next_offset = index + 1
    position = f"next_offset={next_offset}" if next_offset < len(lines) else "EOF"
    description = (
        f"本轮历史原文快照分页视图：超长行每 {HISTORY_VIEW_LINE_CHARS} 字符软换行，offset 为该视图的零基行号；"
        f"本次读取 [{min(offset, len(lines))}, {next_offset})，当前共 {len(lines)} 行，{position}。"
        "若有 next_offset，请保持 file_path 不变并用该 offset 继续；本页不代表已读完全文。"
    )
    content = description + "\n" + "\n".join(rows)
    return ToolMessage(content=content, tool_call_id=tool_call["id"], name="read_file")
