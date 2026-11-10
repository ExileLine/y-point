# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:43:54
# @Author  : yangyuexiong
# @File    : test_m51_live_acceptance.py

"""在隔离业务库通过真实模型、独立 worker 与 Docker 验证 M5.1 组合业务。"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import get_config
from app.core.enums import ModelCapability, ProviderKind, UserRole
from app.models.admin import Admin
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.tool_environments import prepare_next_tool_environment
from tests.test_m51_governance import _database, _http_platform
from tests.test_tool_environments import _wheel


@pytest.mark.skipif(os.environ.get("YPOINT_TEST_OLLAMA_M51") != "1", reason="需显式启用真实 Ollama、Docker 与公网 MCP 验收")
def test_live_skill_dependency_tool_versions_and_mcp_approval(tmp_path: Path, monkeypatch) -> None:
    """两用户完成依赖准备、Skill 引导、直接调用、旧版本固定及 MCP 审批恢复。"""
    config = get_config()
    wheels = tmp_path / "wheels"
    _wheel(wheels, "ypm51demo", "1.0", "def double(value): return value * 2\n")
    for key, value in (
        ("SCRIPT_DEPENDENCY_STORAGE_DIR", str(tmp_path / "environments")),
        ("SCRIPT_DEPENDENCY_ALLOW_NETWORK", False),
        ("SCRIPT_DEPENDENCY_WHEELHOUSE", str(wheels)),
        ("TOOL_STORAGE_MIN_FREE_BYTES", 0),
        ("ARTIFACT_STORAGE_DIR", str(tmp_path / "artifacts")),
    ):
        monkeypatch.setattr(config, key, value)
    env = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(tmp_path / "governance.db"),
        "CHECKPOINT_BACKEND": "sqlite",
        "SCRIPT_DEPENDENCY_STORAGE_DIR": config.SCRIPT_DEPENDENCY_STORAGE_DIR,
        "SCRIPT_DEPENDENCY_ALLOW_NETWORK": "false",
        "SCRIPT_DEPENDENCY_WHEELHOUSE": str(wheels),
        "ARTIFACT_STORAGE_DIR": str(tmp_path / "artifacts"),
        "WORKER_LOG_FILE": str(tmp_path / "worker.log"),
        "DEBUG": "false",
    }

    async def worker_once():
        """每轮启动独立正式 worker，验证数据库队列与检查点恢复。"""
        result = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-m", "app.cli.run_worker", "--once", "--checkpoint-db", str(tmp_path / "checkpoints.db")],
            env=env,
            capture_output=True,
            timeout=150,
        )
        assert result.returncode == 0, "独立 worker 执行失败，请核对隔离日志中的脱敏错误类别"

    async def exercise():
        """使用真实 HTTP 业务接口和两个临时账号完成组合链。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            async with sessions.begin() as db:
                admin = await db.get(Admin, 1)
                admin.role = UserRole.ADMIN
                db.add(
                    ProviderConnection(
                        id=1,
                        name="m51-ollama",
                        name_key="m51-ollama",
                        kind=ProviderKind.OLLAMA,
                        base_url="http://127.0.0.1:11434/v1",
                        timeout_seconds=120,
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="gemma4:31b-cloud",
                        display_name="Gemma4",
                        capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                        verified_capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                        capabilities_verified=True,
                        context_window=32768,
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    ),
                )
            source = "import json,sys,ypm51demo\nprint(json.dumps({'result': ypm51demo.double(json.load(sys.stdin)['value'])}))\n"
            created = await client.post(
                "/api/v1/tools",
                headers=owner,
                json={
                    "name": "m51_double",
                    "description": "返回整数的两倍",
                    "kind": "script",
                    "script_language": "python",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "value": {
                                "type": "integer",
                            },
                        },
                        "required": ["value"],
                    },
                    "source_code": source,
                    "requirements": ["ypm51demo==1.0"],
                },
            )
            assert created.status_code == 201, created.text
            tool_id = created.json()["data"]["id"]
            assert (await client.get(f"/api/v1/tools/{tool_id}", headers=other)).status_code == 404
            assert await prepare_next_tool_environment(sessions)
            tested = await client.post(
                f"/api/v1/tools/{tool_id}/test",
                headers=owner,
                json={
                    "arguments": {
                        "value": 21,
                    },
                },
            )
            assert tested.status_code == 200, tested.text
            assert "42" in tested.json()["data"]["result"]
            published = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 1,
                    "visibility": "public",
                },
            )
            assert published.status_code == 200, published.text
            skill = await client.post(
                "/api/v1/skills",
                headers=owner,
                json={
                    "visibility": "public",
                    "files": {
                        "SKILL.md": (
                            "---\nname: m51-double\ndescription: 调用工具计算整数的两倍。\n---\n"
                            "先读取本技能，然后调用 m51_double，参数 value 为用户给出的整数。请报告工具实际返回的 result。"
                        ),
                    },
                },
            )
            assert skill.status_code == 201, skill.text
            skill_id = skill.json()["data"]["id"]

            async def create_session(tool_ids, skill_ids, prompt):
                """以第二用户创建自己的 Agent 和隔离会话。"""
                agent = await client.post(
                    "/api/v1/agents",
                    headers=other,
                    json={
                        "name": "M51 live",
                        "default_model_id": 1,
                        "max_output_tokens": 256,
                        "max_model_calls": 6,
                        "max_tool_calls": 5,
                        "tool_ids": tool_ids,
                        "skill_ids": skill_ids,
                        "system_prompt": prompt,
                    },
                )
                assert agent.status_code == 201, agent.text
                agent_id = agent.json()["data"]["id"]
                session = await client.post(
                    f"/api/v1/agents/{agent_id}/sessions",
                    headers=other,
                    json={
                        "title": "M51 acceptance",
                    },
                )
                assert session.status_code == 201, session.text
                return f"/api/v1/agents/{agent_id}/sessions/{session.json()['data']['id']}"

            async def run_round(base, instruction, *, mcp=False):
                """经独立进程执行；MCP 必须先审批，再由新进程恢复。"""
                submitted = await client.post(
                    f"{base}/runs",
                    headers=other,
                    json={
                        "input_text": instruction,
                        "request_key": uuid4().hex,
                    },
                )
                assert submitted.status_code == 201, submitted.text
                run_id = submitted.json()["data"]["id"]
                await worker_once()
                state = (await client.get(f"/api/v1/runs/{run_id}", headers=other)).json()["data"]
                if mcp:
                    assert state["state"] == "waiting_for_approval", state.get("failure_code")
                    assert (await client.get(f"/api/v1/runs/{run_id}/approval", headers=owner)).status_code == 404
                    decision = await client.post(
                        f"/api/v1/runs/{run_id}/approval/decision",
                        headers=other,
                        json={
                            "decision": "approve",
                        },
                    )
                    assert decision.status_code == 200, decision.text
                    await worker_once()
                    state = (await client.get(f"/api/v1/runs/{run_id}", headers=other)).json()["data"]
                assert state["state"] == "completed", (state["state"], state.get("failure_code"))
                calls = (await client.get(f"/api/v1/tools/runs/{run_id}/calls", headers=other)).json()["data"]
                assert len(calls) == 1 and calls[0]["state"] == "completed", calls
                messages = (await client.get(f"/api/v1/runs/{run_id}/messages", headers=other)).json()["data"]
                text = next(item["content"] for item in messages if item["role"] == "assistant")
                assert text
                return calls, text

            base = await create_session([tool_id], [skill_id], "必须先读取关联的 m51-double Skill，再调用其中的 m51_double 工具。只使用实际工具结果。")
            calls, text = await run_round(base, "请按 m51-double Skill 计算整数 21 的两倍，必须实际调用 m51_double。")
            assert calls[0]["tool_version"] == 2 and "42" in text
            changed = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 2,
                    "source_code": "import json; print(json.dumps({'result': 63}))",
                },
            )
            assert changed.status_code == 200, changed.text
            assert (
                await client.post(
                    f"/api/v1/tools/{tool_id}/test",
                    headers=owner,
                    json={
                        "arguments": {
                            "value": 21,
                        },
                    },
                )
            ).status_code == 200
            republished = await client.put(
                f"/api/v1/tools/{tool_id}",
                headers=owner,
                json={
                    "expected_version": 3,
                    "visibility": "public",
                },
            )
            assert republished.status_code == 200, republished.text
            calls, text = await run_round(base, "再次实际调用 m51_double 计算 21 的两倍，报告实际 result。")
            assert calls[0]["tool_version"] == 2 and "42" in text
            direct = await create_session([tool_id], [], "直接调用关联的 m51_double 工具并报告实际结果，不需要 Skill。")
            calls, text = await run_round(direct, "实际调用 m51_double，参数 value=21，告诉我工具返回的 result。")
            assert calls[0]["tool_version"] == 4 and "63" in text
            mcp = await client.post(
                "/api/v1/tools",
                headers=owner,
                json={
                    "name": "m51_wiki",
                    "description": "读取公开仓库的 DeepWiki 文档结构",
                    "kind": "mcp",
                    "mcp_url": "https://mcp.deepwiki.com/mcp",
                    "mcp_tool_name": "read_wiki_structure",
                    "effect": "read",
                    "visibility": "public",
                },
            )
            assert mcp.status_code == 201, mcp.text
            mcp_id = mcp.json()["data"]["id"]
            mcp_base = await create_session([mcp_id], [], "必须调用关联的 DeepWiki 工具读取用户给出的仓库结构，审批后简短报告主题。")
            await run_round(mcp_base, "请实际调用 DeepWiki 工具读取 modelcontextprotocol/python-sdk 的文档结构。", mcp=True)

    asyncio.run(exercise())
