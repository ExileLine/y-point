# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 16:12:00
# @Author  : yangyuexiong
# @File    : accept_m5_live.py

"""通过本地 HTTP API 验收两用户 Tool 权限与真实脚本对话。"""

import argparse
import getpass
import time
from typing import Any
from uuid import uuid4

import httpx

BASE_URL = "http://127.0.0.1:7769/api/v1"
TERMINAL_STATES = {"completed", "failed", "cancelled", "timed_out"}


def request(client: httpx.Client, method: str, path: str, token: str, *, expected: int = 200, payload: dict | None = None) -> Any:
    """调用 API 并只在失败时显示脱敏状态和业务消息。"""
    response = client.request(
        method,
        path,
        headers={
            "Authorization": f"Bearer {token}",
        },
        json=payload,
    )
    body = response.json()
    if response.status_code != expected:
        raise AssertionError(f"{method} {path}: HTTP {response.status_code}, {body.get('message')}")
    return body.get("data") or {}


def login(client: httpx.Client, username: str) -> str:
    """从终端读取密码，获取仅留在进程内存中的访问令牌。"""
    password = getpass.getpass(f"{username} 密码: ")
    response = client.post(
        "/auth/login",
        json={
            "identifier": username,
            "password": password,
        },
    )
    if response.status_code != 200:
        raise AssertionError(f"{username} 登录失败: HTTP {response.status_code}")
    return response.json()["data"]["access_token"]


def cleanup_resources(
    client: httpx.Client,
    owner: str,
    other: str,
    *,
    tool_id: int | None,
    agent_id: int | None,
    session_id: str | None,
    run_id: str | None,
) -> None:
    """按会话、Agent、Tool 的顺序清理本次测试资源。"""
    if run_id is not None:
        current = request(client, "GET", f"/runs/{run_id}", other)
        if current["state"] not in TERMINAL_STATES:
            request(client, "POST", f"/runs/{run_id}/cancel", other)
    if session_id is not None and agent_id is not None:
        request(client, "DELETE", f"/agents/{agent_id}/sessions/{session_id}", other)
    if agent_id is not None:
        request(client, "DELETE", f"/agents/{agent_id}", other)
    if tool_id is not None:
        request(client, "DELETE", f"/tools/{tool_id}", owner)


def await_state(client: httpx.Client, token: str, run_id: str, states: set[str], timeout_seconds: int = 120) -> dict:
    """等待 Run 进入指定状态，超时后报告最后的状态与故障类别。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        run = request(client, "GET", f"/runs/{run_id}", token)
        if run["state"] in states:
            return run
        time.sleep(2)
    raise AssertionError(f"等待 Run 状态超时: {run['state']}/{run.get('failure_code')}")


def accept() -> None:
    """验证脚本发布、跨用户授权、无 Skill 调用及审计，并清理临时对象。"""
    suffix = uuid4().hex[:8]
    tool_id = agent_id = session_id = run_id = None
    with httpx.Client(base_url=BASE_URL, follow_redirects=True, timeout=20) as client:
        owner = login(client, "admin")
        other = login(client, "yyx")
        try:
            created = request(
                client,
                "POST",
                "/tools",
                owner,
                expected=201,
                payload={
                    "name": f"m5_double_{suffix}",
                    "description": "将输入整数乘以二，用户要求计算双倍数值时调用",
                    "kind": "script",
                    "visibility": "private",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "integer",
                            },
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                    "script_language": "python",
                    "source_code": "import json, sys\na = json.load(sys.stdin)\nprint(json.dumps({'result': a['value'] * 2}))\n",
                },
            )
            tool_id = created["id"]
            assert created["verified"] is False and created["enabled"] is False
            request(client, "GET", f"/tools/{tool_id}", other, expected=404)
            denied = client.post(
                "/agents",
                headers={
                    "Authorization": f"Bearer {other}",
                },
                json={
                    "name": f"M5 denied {suffix}",
                    "default_model_id": 2,
                    "tool_ids": [tool_id],
                },
            )
            assert denied.status_code == 404, f"未验证 Tool 被关联: HTTP {denied.status_code}"
            print("私有与未验证权限: 通过")

            tested = request(
                client,
                "POST",
                f"/tools/{tool_id}/test",
                owner,
                payload={
                    "arguments": {
                        "value": 21,
                    },
                },
            )
            assert "42" in tested["result"]
            published = request(
                client,
                "PUT",
                f"/tools/{tool_id}",
                owner,
                payload={
                    "expected_version": 1,
                    "visibility": "public",
                },
            )
            assert published["verified"] is True and published["enabled"] is True
            public = request(client, "GET", f"/tools/{tool_id}", other)
            assert "source_code" not in public and public["visibility"] == "public"
            print("沙箱试运行与公开发布: 通过")

            agent = request(
                client,
                "POST",
                "/agents",
                other,
                expected=201,
                payload={
                    "name": f"M5 Tool {suffix}",
                    "default_model_id": 2,
                    "max_output_tokens": 256,
                    "temperature": "0",
                    "max_model_calls": 3,
                    "max_tool_calls": 2,
                    "skill_ids": [],
                    "tool_ids": [tool_id],
                    "system_prompt": "必须调用已关联的 double 工具计算用户请求的双倍值，然后用中文简短回答。",
                },
            )
            agent_id = agent["id"]
            assert agent["tool_ids"] == [tool_id] and agent["skill_ids"] == []
            session = request(
                client,
                "POST",
                f"/agents/{agent_id}/sessions",
                other,
                expected=201,
                payload={
                    "title": "M5 验收",
                },
            )
            session_id = session["id"]
            run = request(
                client,
                "POST",
                f"/agents/{agent_id}/sessions/{session_id}/runs",
                other,
                expected=201,
                payload={
                    "input_text": "请调用关联的工具，传入 value=21，并告诉我结果。",
                    "request_key": f"m5-{suffix}",
                },
            )
            run_id = run["id"]
            state = await_state(client, other, run_id, TERMINAL_STATES, timeout_seconds=100)
            calls = request(client, "GET", f"/tools/runs/{run_id}/calls", other)
            assert state["state"] == "completed", f"运行失败: {state['state']}/{state.get('failure_code')}"
            assert len(calls) == 1 and calls[0]["state"] == "completed", f"Tool 调用异常: {calls}"
            messages = request(client, "GET", f"/runs/{run_id}/messages", other)
            assert any("42" in (item.get("content") or "") for item in messages), "最终回答未包含脚本结果"
            print(f"无 Skill 真实对话与调用审计: 通过 (run_id={run_id})")
        finally:
            cleanup_resources(
                client,
                owner,
                other,
                tool_id=tool_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
            )


def accept_mcp() -> None:
    """验证真实 MCP 工具在共享授权、审批恢复与调用审计中的完整链路。"""
    suffix = uuid4().hex[:8]
    tool_id = agent_id = session_id = run_id = None
    with httpx.Client(base_url=BASE_URL, follow_redirects=True, timeout=20) as client:
        owner = login(client, "admin")
        other = login(client, "yyx")
        try:
            created = request(
                client,
                "POST",
                "/tools",
                owner,
                expected=201,
                payload={
                    "name": f"m5_wiki_{suffix}",
                    "description": "读取公开 GitHub 仓库的 DeepWiki 文档结构",
                    "kind": "mcp",
                    "visibility": "private",
                    "mcp_url": "https://mcp.deepwiki.com/mcp",
                    "mcp_tool_name": "read_wiki_structure",
                },
            )
            tool_id = created["id"]
            assert created["verified"] is True and created["requires_approval"] is True
            request(client, "GET", f"/tools/{tool_id}", other, expected=404)
            published = request(
                client,
                "PUT",
                f"/tools/{tool_id}",
                owner,
                payload={
                    "expected_version": 1,
                    "visibility": "public",
                },
            )
            assert published["visibility"] == "public"
            remote = request(client, "GET", f"/tools/{tool_id}", other)
            assert "mcp_url" not in remote
            print("MCP 发现、私有隔离与公开发布: 通过")

            agent = request(
                client,
                "POST",
                "/agents",
                other,
                expected=201,
                payload={
                    "name": f"M5 MCP {suffix}",
                    "default_model_id": 2,
                    "max_output_tokens": 256,
                    "temperature": "0",
                    "max_model_calls": 3,
                    "max_tool_calls": 2,
                    "skill_ids": [],
                    "tool_ids": [tool_id],
                    "system_prompt": "必须先调用已关联的 DeepWiki 工具读取公开仓库的文档结构，然后用中文简短总结。",
                },
            )
            agent_id = agent["id"]
            session = request(
                client,
                "POST",
                f"/agents/{agent_id}/sessions",
                other,
                expected=201,
                payload={
                    "title": "M5 MCP 验收",
                },
            )
            session_id = session["id"]
            run = request(
                client,
                "POST",
                f"/agents/{agent_id}/sessions/{session_id}/runs",
                other,
                expected=201,
                payload={
                    "input_text": "请调用 DeepWiki 工具，读取 modelcontextprotocol/python-sdk 的文档结构，再告诉我有哪些主题。",
                    "request_key": f"m5-mcp-{suffix}",
                },
            )
            run_id = run["id"]
            state = await_state(client, other, run_id, {"waiting_for_approval", *TERMINAL_STATES})
            assert state["state"] == "waiting_for_approval", f"未进入审批: {state['state']}/{state.get('failure_code')}"
            request(client, "GET", f"/runs/{run_id}/approval", owner, expected=404)
            approval = request(client, "GET", f"/runs/{run_id}/approval", other)
            assert approval["state"] == "pending" and approval["actions"][0]["name"] == created["model_name"]
            request(
                client,
                "POST",
                f"/runs/{run_id}/approval/decision",
                other,
                payload={
                    "decision": "approve",
                },
            )
            print("MCP 审批与跨用户隔离: 通过")

            state = await_state(client, other, run_id, TERMINAL_STATES)
            calls = request(client, "GET", f"/tools/runs/{run_id}/calls", other)
            assert state["state"] == "completed", f"MCP 运行失败: {state['state']}/{state.get('failure_code')}"
            assert len(calls) == 1 and calls[0]["state"] == "completed", f"MCP 调用异常: {calls}"
            messages = request(client, "GET", f"/runs/{run_id}/messages", other)
            assert any(item["role"] == "assistant" and item["content"] for item in messages)
            print(f"MCP 审批恢复、真实调用与审计: 通过 (run_id={run_id})")
        finally:
            cleanup_resources(
                client,
                owner,
                other,
                tool_id=tool_id,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
            )


def accept_skill() -> None:
    """验证标准 Skill 包在真实模型运行中按需读取并留下版本事件。"""
    suffix = uuid4().hex[:8]
    skill_name = f"m5-marker-{suffix}"
    marker = f"M5-CHECK-{suffix.upper()}"
    skill_id = agent_id = session_id = run_id = None
    with httpx.Client(base_url=BASE_URL, follow_redirects=True, timeout=20) as client:
        owner = login(client, "admin")
        other = login(client, "yyx")
        try:
            created = request(
                client,
                "POST",
                "/skills",
                owner,
                expected=201,
                payload={
                    "visibility": "private",
                    "files": {
                        "SKILL.md": (
                            f"---\nname: {skill_name}\ndescription: 查询 M5 验收标记时使用。\n---\n\n"
                            "# 验收标记\n\n请读取本包的 facts.txt，再向用户报告其中的标记。\n"
                        ),
                        "facts.txt": f"验收标记：{marker}\n",
                    },
                },
            )
            skill_id = created["id"]
            request(client, "GET", f"/skills/{skill_id}", other, expected=404)
            exported = client.get(
                f"/skills/{skill_id}/export",
                headers={
                    "Authorization": f"Bearer {owner}",
                },
            )
            assert exported.status_code == 200 and exported.content.startswith(b"PK"), "Skill ZIP 导出失败"
            print("Skill 私有隔离与 ZIP 导出: 通过")

            agent = request(
                client,
                "POST",
                "/agents",
                owner,
                expected=201,
                payload={
                    "name": f"M5 Skill {suffix}",
                    "default_model_id": 2,
                    "max_output_tokens": 256,
                    "temperature": "0",
                    "max_model_calls": 3,
                    "max_tool_calls": 2,
                    "skill_ids": [skill_id],
                    "tool_ids": [],
                    "system_prompt": f"先使用 read_file 读取 /skills/{skill_name}/facts.txt，再原样回答其中的验收标记。",
                },
            )
            agent_id = agent["id"]
            session = request(
                client,
                "POST",
                f"/agents/{agent_id}/sessions",
                owner,
                expected=201,
                payload={
                    "title": "M5 Skill 验收",
                },
            )
            session_id = session["id"]
            run = request(
                client,
                "POST",
                f"/agents/{agent_id}/sessions/{session_id}/runs",
                owner,
                expected=201,
                payload={
                    "input_text": "请从关联的 Skill 文件读取并告诉我验收标记。",
                    "request_key": f"m5-skill-{suffix}",
                },
            )
            run_id = run["id"]
            state = await_state(client, owner, run_id, TERMINAL_STATES)
            assert state["state"] == "completed", f"Skill 运行失败: {state['state']}/{state.get('failure_code')}"
            messages = request(client, "GET", f"/runs/{run_id}/messages", owner)
            assert any(marker in item["content"] for item in messages if item["role"] == "assistant"), "未读到 Skill 文件标记"
            events = request(client, "GET", f"/runs/{run_id}/events?after=0", owner)
            assert any((item.get("tool_name") or "").startswith(f"skill:{skill_name}@v1") for item in events), "缺少 Skill 版本事件"
            print(f"Skill 真实模型按需读取与版本事件: 通过 (run_id={run_id})")
        finally:
            cleanup_resources(
                client,
                owner,
                owner,
                tool_id=None,
                agent_id=agent_id,
                session_id=session_id,
                run_id=run_id,
            )
            if skill_id is not None:
                request(client, "DELETE", f"/skills/{skill_id}", owner)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="验收 M5 Tool 链路或清理中断后的测试资源")
    parser.add_argument("--cleanup-tool", type=int, help="清理指定 Tool ID")
    parser.add_argument("--cleanup-agent", type=int, help="清理指定 Agent ID")
    parser.add_argument("--cleanup-session", help="清理指定会话 ID")
    parser.add_argument("--mcp", action="store_true", help="验收真实 MCP 审批和调用")
    parser.add_argument("--skill", action="store_true", help="验收真实模型 Skill 按需读取")
    args = parser.parse_args()
    if args.cleanup_tool or args.cleanup_agent or args.cleanup_session:
        with httpx.Client(base_url=BASE_URL, follow_redirects=True, timeout=20) as client:
            owner_token = login(client, "admin")
            other_token = login(client, "yyx")
            cleanup_resources(
                client,
                owner_token,
                other_token,
                tool_id=args.cleanup_tool,
                agent_id=args.cleanup_agent,
                session_id=args.cleanup_session,
                run_id=None,
            )
        print("指定资源已清理")
    elif args.mcp:
        accept_mcp()
    elif args.skill:
        accept_skill()
    else:
        accept()
