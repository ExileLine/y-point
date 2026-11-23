# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 18:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_connection_sharing.py

"""通过实际 HTTP 接口和隔离事务验证连接共享、选择、版本固定与撤权。"""

import asyncio
import copy
from pathlib import Path

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import RunState
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.base import Base
from app.models.mcp_connection import AgentMcpTool
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.skill import AgentSkill, Skill, SkillVersion
from app.models.tool import AgentTool, RunTool
from app.services.session_models import requires_tools
from app.services.run_manager import create_run
from app.services.user_tools import load_run_tools, snapshot_run_tools


@pytest.mark.parametrize("transport", ["streamable_http", "stdio"])
def test_connection_selection_sharing_revocation_and_upgrade(tmp_path: Path, monkeypatch, transport: str) -> None:
    """17 工具子集固定、重复去重、实际使用者鉴权和授权撤销贯穿正式接口。"""
    monkeypatch.setattr(get_config(), "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(
        get_config(),
        "MCP_STDIO_PROFILES",
        {
            "fixture": {
                "image": "sha256:" + "a" * 64,
                "command": ["python", "server.py"],
                "env_keys": ["TOKEN"],
            },
        },
    )
    monkeypatch.setattr("app.services.user_tools.validate_mcp_url", lambda url: "127.0.0.1")
    definitions = [
        {
            "name": f"search_{index}",
            "description": f"查询 {index}",
            "input_schema": {
                "type": "object",
                "properties": {},
            },
            "read_only": True,
            "annotations": {
                "readOnlyHint": True,
            },
        }
        for index in range(17)
    ]

    async def discover(*args):
        """只控制远端目录，授权、发布、数据库与运行快照使用实际实现。"""
        return copy.deepcopy(definitions)

    monkeypatch.setattr("app.api.v1.mcp_connections.discover_mcp_tools", discover)
    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def finite_stream(run_id, *args, **kwargs):
        """隔离持续 SSE 订阅，提交、权限、会话版本与快照仍走正式 HTTP 链路。"""
        return api_response(
            data={
                "run_id": run_id,
            }
        )

    monkeypatch.setattr("app.api.v1.ag_ui.build_run_stream", finite_stream)

    async def exercise():
        """每个请求独立事务，切换真实用户身份校验权限隔离。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sharing.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actors = [Admin(id=index, username=f"member-{index}", password="unused", role="user", is_deleted=0, status=1) for index in (1, 2, 3)]
        current = [actors[0]]

        async def database():
            """提供隔离业务事务，失败请求不残留修改。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_current_admin] = lambda: current[0]
        app.dependency_overrides[get_db_session] = database
        try:
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(actors)
                db.add(ProviderConnection(id=1, creator_id=1, name="fixture", name_key="fixture", kind="openai", base_url="https://model.invalid/v1"))
                db.add(
                    ModelCatalog(
                        id=1,
                        creator_id=1,
                        provider_id=1,
                        external_id="model",
                        display_name="测试模型",
                        capabilities=["text", "tools"],
                        verified_capabilities=["text", "tools"],
                    )
                )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

                async def request(method, path, body=None, *, status=200):
                    """统一断言真实 HTTP 状态与业务响应。"""
                    response = await client.request(method, "/api/v1" + path, json=body)
                    assert response.status_code == status, response.text
                    return response.json().get("data")

                draft = {
                    "name": "私有连接",
                    "transport": transport,
                    "share_credentials": False,
                }
                if transport == "streamable_http":
                    draft.update(
                        {
                            "url": "http://secret.internal:4321/mcp",
                            "auth_type": "api_key",
                            "api_key": "private-value",
                            "api_key_header": "X-Secret-Key",
                            "sensitive_headers": {
                                "X-Tenant-Secret": "tenant-value",
                            },
                        }
                    )
                else:
                    draft.update(
                        {
                            "stdio_profile": "fixture",
                            "env": {
                                "TOKEN": "private-value",
                            },
                        }
                    )
                connection = await request("POST", "/mcp-connections", draft, status=201)
                cid = connection["id"]

                async def publish(key, names):
                    """明确同步选定工具，不直接写入工具表。"""
                    plan = await request("POST", f"/mcp-connections/{cid}/sync-preview")
                    return await request(
                        "POST",
                        f"/mcp-connections/{cid}/sync",
                        {
                            "request_id": key,
                            "expected_version": plan["connection_version"],
                            "preview_hash": plan["preview_hash"],
                            "selections": [
                                {
                                    "remote_name": row["remote_name"],
                                    "tool_id": row["tool_id"],
                                    "expected_version": row["current_version"],
                                }
                                for row in plan["items"]
                                if row["remote_name"] in names
                            ],
                        },
                    )

                await publish("first", [tool["name"] for tool in definitions])
                catalog = await request("GET", "/mcp-connections/catalog")
                assert len(catalog) == 1 and len(catalog[0]["tools"]) == 17
                assert not any(secret in str(catalog) for secret in ("secret.internal", "private-value", "tenant-value", "server.py", "X-Secret-Key", "TOKEN"))
                chosen = [
                    {
                        "tool_id": tool["tool_id"],
                        "tool_version": tool["tool_version"],
                    }
                    for tool in catalog[0]["tools"][:10]
                ]
                tid = chosen[0]["tool_id"]
                groups = [
                    {
                        "connection_id": cid,
                        "tools": chosen,
                    }
                ]
                for invalid, status in [
                    (
                        [
                            {
                                "connection_id": cid,
                                "tools": [],
                            }
                        ],
                        400,
                    ),
                    (groups + groups, 400),
                    (
                        [
                            {
                                "connection_id": cid,
                                "tools": [chosen[0], chosen[0]],
                            }
                        ],
                        400,
                    ),
                    (
                        [
                            {
                                "connection_id": cid,
                                "tools": [
                                    {
                                        "tool_id": tid,
                                        "tool_version": 999,
                                    }
                                ],
                            }
                        ],
                        409,
                    ),
                    (
                        [
                            {
                                "connection_id": cid + 100,
                                "tools": chosen,
                            }
                        ],
                        404,
                    ),
                ]:
                    await request(
                        "POST",
                        "/agents",
                        {
                            "name": "非法连接选择",
                            "default_model_id": 1,
                            "mcp_connections": invalid,
                        },
                        status=status,
                    )
                await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": 0,
                        "tool_ids": [tid, tid],
                    },
                    status=400,
                )
                await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": 0,
                        "tool_ids": [99999],
                    },
                    status=409,
                )
                await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/1",
                    {
                        "expected_version": 0,
                        "tool_ids": [],
                    },
                    status=404,
                )
                owner_agent = await request(
                    "POST",
                    "/agents",
                    {
                        "name": "按连接选择",
                        "default_model_id": 1,
                        "tool_ids": [tid],
                        "mcp_connections": groups,
                    },
                    status=201,
                )
                direct_agent = await request(
                    "POST",
                    "/agents",
                    {
                        "name": "旧直接关联",
                        "default_model_id": 1,
                        "tool_ids": [tid],
                    },
                    status=201,
                )
                aid = owner_agent["id"]
                assert owner_agent["mcp_connections"] == groups and owner_agent["tool_ids"] == [tid]
                async with sessions.begin() as db:
                    owner_run = await create_run(db, 1, agent_id=aid, input_text="owner")
                    await snapshot_run_tools(db, owner_run.id, aid, 1)
                    snapshots = list(await db.scalars(select(RunTool).where(RunTool.run_id == owner_run.id)))
                    assert len(snapshots) == 10 and sum(row.mcp_connection_id == cid for row in snapshots) == 9
                    old_run_id = owner_run.id
                await request(
                    "POST",
                    f"/agents/{aid}/shares",
                    {
                        "user_id": 2,
                    },
                )
                current[0] = actors[1]
                assert await request("GET", "/mcp-connections/catalog") == []
                assert len(await request("GET", "/agents/shared")) == 1
                async with sessions() as db:
                    run = await create_run(db, 2, agent_id=aid)
                    with pytest.raises(CustomException) as denied:
                        await snapshot_run_tools(db, run.id, aid, 2)
                    assert "共享 Agent 不包含连接权限" in denied.value.detail
                    await db.rollback()
                shared_session = await request("POST", f"/agents/{aid}/sessions", {}, status=201)
                await request(
                    "POST",
                    f"/agents/{aid}/ag-ui",
                    {
                        "threadId": shared_session["id"],
                        "runId": "missing-connection",
                        "protocolVersion": "1.0",
                        "messages": [
                            {
                                "id": "missing-user",
                                "role": "user",
                                "content": "请查询",
                            }
                        ],
                    },
                    status=409,
                )
                async with sessions() as db:
                    assert await db.scalar(select(AgentRun).where(AgentRun.session_id == shared_session["id"])) is None
                await request("GET", f"/mcp-connections/{cid}/shares", status=404)
                await request("POST", f"/mcp-connections/{cid}/discover", status=404)
                current[0] = actors[0]
                share = await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": 0,
                        "tool_ids": [tool["tool_id"] for tool in chosen],
                        "allow_credentials": False,
                    },
                )
                assert share["version"] == 1
                current[0] = actors[1]
                directory = await request("GET", "/mcp-connections/catalog")
                assert len(directory[0]["tools"]) == 10 and directory[0]["can_edit"] is False
                assert all(tool["callability"]["status"] == "blocked" for tool in directory[0]["tools"])
                assert directory[0]["share_version"] == 1 and directory[0]["allow_credentials"] is False
                for entry in directory[0]["tools"]:
                    detail = await request("GET", f"/tools/{entry['tool_id']}")
                    assert detail["callability"] == entry["callability"]
                    assert entry["callability"]["reason"] == "credentials_not_shared"
                    assert entry["mcp_connection_version"] == detail["mcp_connection_version"]
                    assert entry["callability_checked_at"] and detail["callability_checked_at"]
                    assert not any(key in detail for key in ("mcp_url", "source_code", "mcp_annotations"))
                await request(
                    "POST",
                    "/agents",
                    {
                        "name": "缺借用权限",
                        "default_model_id": 1,
                        "mcp_connections": groups,
                    },
                    status=409,
                )
                current[0] = actors[0]
                share = await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": 1,
                        "tool_ids": [tool["tool_id"] for tool in chosen],
                        "allow_credentials": True,
                    },
                )
                current[0] = actors[1]
                recipient_agent = await request(
                    "POST",
                    "/agents",
                    {
                        "name": "独立授权",
                        "default_model_id": 1,
                        "mcp_connections": groups,
                    },
                    status=201,
                )
                rid = recipient_agent["id"]
                details = await request("GET", f"/tools/{tid}")
                assert "mcp_url" not in details and "source_code" not in details and "private-value" not in str(details)
                async with sessions.begin() as db:
                    assert await requires_tools(db, rid)
                    skill = Skill(
                        owner_id=2,
                        name="group-dependency",
                        description="连接工具依赖",
                        visibility="private",
                        enabled=True,
                        current_version=1,
                        is_deleted=0,
                        status=1,
                    )
                    db.add(skill)
                    await db.flush()
                    db.add(
                        SkillVersion(
                            skill_id=skill.id,
                            version=1,
                            content_hash="a" * 64,
                            files={
                                "y-point.yaml": f"required_tools: [{details['model_name']}]\n",
                            },
                            manifest=[
                                {
                                    "package_file": True,
                                }
                            ],
                        )
                    )
                    db.add(AgentSkill(agent_id=rid, skill_id=skill.id, skill_version=1))
                own_session = await request("POST", f"/agents/{rid}/sessions", {}, status=201)
                submitted = await request(
                    "POST",
                    f"/agents/{rid}/ag-ui",
                    {
                        "threadId": own_session["id"],
                        "runId": "authorized-connection",
                        "protocolVersion": "1.0",
                        "messages": [
                            {
                                "id": "authorized-user",
                                "role": "user",
                                "content": "请查询",
                            }
                        ],
                    },
                )
                async with sessions() as db:
                    fixed = list(await db.scalars(select(RunTool).where(RunTool.run_id == submitted["run_id"])))
                    assert len(fixed) == 10 and all(row.mcp_connection_id == cid for row in fixed)
                tool_catalog = await request("GET", "/tools/catalog")
                assert len(tool_catalog["user_tools"]) == 10
                directory = await request("GET", "/mcp-connections/catalog")
                assert directory[0]["share_version"] == 2 and directory[0]["allow_credentials"] is True
                for entry in directory[0]["tools"]:
                    summary = next(item for item in tool_catalog["user_tools"] if item["id"] == entry["tool_id"])
                    assert entry["callability"] == summary["callability"]
                    assert entry["callability"]["status"] == "direct"
                    assert entry["mcp_connection_version"] == summary["mcp_connection_version"]
                async with sessions.begin() as db:
                    run = await create_run(db, 2, agent_id=rid, input_text="recipient")
                    await snapshot_run_tools(db, run.id, rid, 2)
                    assert len(await load_run_tools(db, run.id, 2)) == 10
                    recipient_run_id = run.id
                    waiting = await create_run(db, 2, agent_id=aid, input_text="approval")
                    await snapshot_run_tools(db, waiting.id, aid, 2)
                    waiting.state = RunState.WAITING_FOR_APPROVAL
                    waiting_id = waiting.id
                current[0] = actors[2]
                assert await request("GET", "/mcp-connections/catalog") == []
                await request("GET", f"/tools/{tid}", status=404)
                current[0] = actors[0]
                # 同步新增工具不会扩大原授权；旧绑定也不会静默升级。
                definitions.append(
                    {
                        **copy.deepcopy(definitions[-1]),
                        "name": "search_17",
                    }
                )
                await publish("new-remote", ["search_17"])
                definitions[0]["input_schema"]["properties"]["project_id"] = {
                    "type": "string",
                }
                await publish("new-version", [definitions[0]["name"]])
                await request(
                    "PUT",
                    f"/agents/{direct_agent['id']}",
                    {
                        "expected_version": direct_agent["version"],
                        "tool_ids": [],
                        "mcp_connections": [
                            {
                                "connection_id": cid,
                                "tools": [
                                    {
                                        "tool_id": tid,
                                        "tool_version": 2,
                                    }
                                ],
                            }
                        ],
                    },
                    status=409,
                )
                converted = await request(
                    "PUT",
                    f"/agents/{direct_agent['id']}",
                    {
                        "expected_version": direct_agent["version"],
                        "tool_ids": [],
                        "mcp_connections": [
                            {
                                "connection_id": cid,
                                "tools": [
                                    {
                                        "tool_id": tid,
                                        "tool_version": 1,
                                    }
                                ],
                            }
                        ],
                    },
                )
                await request(
                    "PUT",
                    f"/agents/{direct_agent['id']}",
                    {
                        "expected_version": converted["version"],
                        "tool_ids": [tid],
                        "mcp_connections": [],
                    },
                )
                async with sessions() as db:
                    assert (await db.scalar(select(AgentTool).where(AgentTool.agent_id == direct_agent["id"]))).tool_version == 1
                owner_agent = await request("GET", f"/agents/{aid}")
                saved = await request(
                    "PUT",
                    f"/agents/{aid}",
                    {
                        "expected_version": owner_agent["version"],
                        "name": "仅改名",
                    },
                )
                assert saved["mcp_connections"][0]["tools"][0]["tool_version"] == 1
                conflict = copy.deepcopy(groups)
                conflict[0]["tools"][0]["tool_version"] = 2
                await request(
                    "PUT",
                    f"/agents/{aid}",
                    {
                        "expected_version": saved["version"],
                        "mcp_connections": conflict,
                    },
                    status=409,
                )
                await request(
                    "POST",
                    "/agents",
                    {
                        "name": "重复版本冲突",
                        "default_model_id": 1,
                        "tool_ids": [tid],
                        "mcp_connections": groups,
                    },
                    status=409,
                )
                plan = await request("GET", f"/agents/{aid}/tool-upgrades")
                await request(
                    "POST",
                    f"/agents/{aid}/tool-upgrades",
                    {
                        "request_id": "upgrade-group-and-direct",
                        "expected_version": plan["agent_version"],
                        "preview_hash": plan["preview_hash"],
                        "selections": [
                            {
                                "tool_id": tid,
                                "expected_version": 1,
                                "target_version": 2,
                            }
                        ],
                    },
                )
                async with sessions() as db:
                    assert (await db.scalar(select(AgentTool).where(AgentTool.agent_id == aid))).tool_version == 2
                    assert (await db.scalar(select(AgentMcpTool).where(AgentMcpTool.agent_id == aid, AgentMcpTool.tool_id == tid))).tool_version == 2
                    old = await db.scalar(select(RunTool).where(RunTool.run_id == old_run_id, RunTool.tool_id == tid))
                    assert old.tool_version == 1
                current[0] = actors[1]
                directory = await request("GET", "/mcp-connections/catalog")
                assert len(directory[0]["tools"]) == 10
                # 子集缩减立即影响旧快照，即使其他工具和凭据权限保留。
                current[0] = actors[0]
                reduced = await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": 2,
                        "tool_ids": share["tool_ids"][:-1],
                        "allow_credentials": True,
                    },
                )
                async with sessions() as db:
                    assert (await db.get(AgentRun, recipient_run_id)).state in (RunState.CANCEL_REQUESTED, RunState.CANCELLED)
                    with pytest.raises(CustomException) as missing:
                        await load_run_tools(db, recipient_run_id, 2)
                    assert "未授权" in missing.value.detail
                share = await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": reduced["version"],
                        "tool_ids": share["tool_ids"],
                        "allow_credentials": True,
                    },
                )
                async with sessions.begin() as db:
                    fresh = await create_run(db, 2, agent_id=rid)
                    await snapshot_run_tools(db, fresh.id, rid, 2)
                    recipient_run_id = fresh.id
                    fresh_waiting = await create_run(db, 2, agent_id=rid)
                    await snapshot_run_tools(db, fresh_waiting.id, rid, 2)
                    fresh_waiting.state = RunState.WAITING_FOR_APPROVAL
                    waiting_id = fresh_waiting.id
                # 借用撤销取消排队及审批中的运行，并在调用前重验。
                current[0] = actors[0]
                share = await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": share["version"],
                        "tool_ids": share["tool_ids"],
                        "allow_credentials": False,
                    },
                )
                async with sessions() as db:
                    for run_id in (recipient_run_id, waiting_id):
                        run = await db.get(AgentRun, run_id)
                        assert run.state in (RunState.CANCEL_REQUESTED, RunState.CANCELLED)
                        with pytest.raises(CustomException):
                            await load_run_tools(db, run_id, 2)
                    assert (await db.get(AgentRun, old_run_id)).state == RunState.QUEUED
                await request("DELETE", f"/mcp-connections/{cid}/shares/2?expected_version=2", status=409)
                await request("DELETE", f"/mcp-connections/{cid}/shares/2?expected_version={share['version']}")
                current[0] = actors[1]
                assert await request("GET", "/mcp-connections/catalog") == []
                assert (await request("GET", "/tools/catalog"))["user_tools"] == []
                # 重授权不复用旧授权版本，旧页面不能撤销新的授权。
                current[0] = actors[0]
                renewed = await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/2",
                    {
                        "expected_version": 0,
                        "tool_ids": [],
                        "allow_credentials": False,
                    },
                )
                assert renewed["version"] > share["version"]
                await request("DELETE", f"/mcp-connections/{cid}/shares/2?expected_version={share['version']}", status=409)
                current[0] = actors[1]
                directory = await request("GET", "/mcp-connections/catalog")
                assert len(directory) == 1 and directory[0]["tools"] == []
                recipient_agent = await request("GET", f"/agents/{rid}")
                # 旧客户端省略新字段时保留；显式空列表可修复已失效关联。
                preserved = await request(
                    "PUT",
                    f"/agents/{rid}",
                    {
                        "expected_version": recipient_agent["version"],
                        "name": "仍保留旧关联",
                    },
                )
                assert len(preserved["mcp_connections"][0]["tools"]) == 10
                await request(
                    "PUT",
                    f"/agents/{rid}",
                    {
                        "expected_version": preserved["version"],
                        "mcp_connections": None,
                    },
                    status=400,
                )
                cleared = await request(
                    "PUT",
                    f"/agents/{rid}",
                    {
                        "expected_version": preserved["version"],
                        "mcp_connections": [],
                        "skill_ids": [],
                    },
                )
                assert cleared["mcp_connections"] == []
                current[0] = actors[0]
                await request(
                    "PUT",
                    f"/mcp-connections/{cid}/shares/3",
                    {
                        "expected_version": 0,
                        "tool_ids": [],
                    },
                )
                concurrent = await asyncio.gather(
                    *[
                        client.put(
                            f"/api/v1/mcp-connections/{cid}/shares/3",
                            json={
                                "expected_version": 1,
                                "tool_ids": [],
                                "allow_credentials": allowed,
                            },
                        )
                        for allowed in (False, True)
                    ]
                )
                assert sorted(response.status_code for response in concurrent) == [200, 409]

        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(exercise())
