# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 10:33:31
# @Author  : yangyuexiong
# @File    : test_skills.py

"""验证 Skill ZIP 边界、跨用户授权和运行版本固定。"""

import asyncio
import io
import stat
import zipfile
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.base import Base
from app.models.skill import AgentSkill, RunSkill
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.skills import export_zip, import_zip, load_run_skill_files, snapshot_run_skills
from app.services.runtime_adapter import DeepAgentsRuntimeAdapter, RuntimeRunConfig
from deepagents.backends.utils import create_file_data


def skill_text(name: str, body: str = "步骤") -> str:
    """构造标准 Skill 说明文件。"""
    return f"---\nname: {name}\ndescription: 测试说明\n---\n\n# {body}\n"


class SkillModel(FakeMessagesListChatModel):
    """允许 DeepAgents 绑定文件读取工具的假模型。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """让测试直接返回预先设置的工具调用。"""
        return self


def test_runtime_adapter_loads_skill_on_demand() -> None:
    """正式适配器注册元数据并允许模型按需读取关联文件。"""

    async def exercise() -> None:
        """执行一轮模型读取 Skill 文件的真实图。"""
        path = "/skills/sample/SKILL.md"
        model = SkillModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": path,
                            },
                            "id": "skill-read",
                        }
                    ],
                ),
                AIMessage(content="done"),
            ]
        )
        config = RuntimeRunConfig("run", 1, "skill-runtime", None, ())
        runtime = DeepAgentsRuntimeAdapter(
            config,
            model=model,
            tools=[],
            interrupt_on={},
            checkpointer=InMemorySaver(),
            middleware=[],
            skill_files={
                path: skill_text("sample"),
            },
        )
        async for _ in runtime.events(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "use sample",
                    }
                ],
                "files": {
                    path: create_file_data(skill_text("sample")),
                },
                "skills_metadata": None,
            }
        ):
            pass
        state = await runtime.state()
        assert [item["name"] for item in state.values["skills_metadata"]] == ["sample"]
        assert any(isinstance(item, ToolMessage) and "测试说明" in str(item.content) for item in state.values["messages"])

    asyncio.run(exercise())


def test_zip_rejects_unsafe_entries() -> None:
    """拒绝路径穿越、软链接、重复名称和超限解压量。"""
    for entry in ("../outside", "/outside", "folder/../outside", "folder\\outside"):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("SKILL.md", skill_text("safe"))
            archive.writestr(entry, "secret")
        with pytest.raises(ValueError):
            import_zip(output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", skill_text("safe"))
        archive.writestr("../outside/", "")
    with pytest.raises(ValueError):
        import_zip(output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", skill_text("safe"))
        linked = zipfile.ZipInfo("linked")
        linked.create_system = 3
        linked.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(linked, "../outside")
    with pytest.raises(ValueError):
        import_zip(output.getvalue())
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("SKILL.md", skill_text("safe"))
        archive.writestr("huge.txt", "x" * (128 * 1024 + 1))
    with pytest.raises(ValueError):
        import_zip(output.getvalue())


def test_skill_api_and_run_authorization(tmp_path: Path) -> None:
    """私有 Skill 不可跨用户关联；公开版本固定并在撤权后拒绝运行加载。"""

    async def exercise() -> None:
        """使用隔离业务库与真实 HTTP 接口完成授权链。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'skills.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(id=101, username="skill-a", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                        Admin(id=102, username="skill-b", password=hash_password("StrongPassword123"), role="user", status=1, is_deleted=0),
                        ProviderConnection(
                            id=201,
                            name="skill-provider",
                            name_key="skill-provider",
                            kind="ollama",
                            base_url="http://localhost:11434/v1",
                            creator_id=101,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=202,
                            provider_id=201,
                            external_id="verified",
                            display_name="Verified",
                            capabilities=["text"],
                            verified_capabilities=["text"],
                            capabilities_verified=True,
                            context_window=8192,
                            creator_id=101,
                            status=1,
                            is_deleted=0,
                        ),
                    ]
                )

            async def session_override():
                """将请求数据库切换到测试库。"""
                async with sessions() as db:
                    yield db

            app.dependency_overrides[get_db_session] = session_override
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

                    async def auth(name: str) -> dict[str, str]:
                        """登录测试用户并返回授权头。"""
                        result = await client.post(
                            "/api/v1/auth/login",
                            json={
                                "identifier": name,
                                "password": "StrongPassword123",
                            },
                        )
                        assert result.status_code == 200
                        return {
                            "Authorization": f"Bearer {result.json()['data']['access_token']}",
                        }

                    alice, bob = await auth("skill-a"), await auth("skill-b")
                    created = await client.post(
                        "/api/v1/skills",
                        headers=alice,
                        json={
                            "files": {
                                "SKILL.md": skill_text("shared"),
                            },
                        },
                    )
                    assert created.status_code == 201, created.text
                    skill_id = created.json()["data"]["id"]
                    assert (await client.get("/api/v1/skills", headers=bob)).json()["data"] == []
                    assert (await client.get(f"/api/v1/skills/{skill_id}", headers=bob)).status_code == 404
                    async with sessions.begin() as db:
                        db.add(AgentSkill(agent_id=300, skill_id=skill_id, skill_version=1))
                        with pytest.raises(Exception):
                            await snapshot_run_skills(db, "run-private", 300, 102)
                    updated = await client.put(
                        f"/api/v1/skills/{skill_id}",
                        headers=alice,
                        json={
                            "expected_version": 1,
                            "visibility": "public",
                            "files": {
                                "SKILL.md": skill_text("shared", "新版"),
                            },
                        },
                    )
                    assert updated.status_code == 200, updated.text
                    assert updated.json()["data"]["current_version"] == 2
                    assert (await client.get("/api/v1/skills", headers=bob)).json()["data"][0]["name"] == "shared"
                    imported = await client.post(
                        "/api/v1/skills/import",
                        headers=alice,
                        files={
                            "file": (
                                "shared.zip",
                                export_zip(
                                    {
                                        "SKILL.md": skill_text("shared", "导入版"),
                                    }
                                ),
                                "application/zip",
                            ),
                        },
                    )
                    assert imported.status_code == 200, imported.text
                    assert imported.json()["data"]["current_version"] == 3
                    assert imported.json()["data"]["visibility"] == "public"
                    created_agent = await client.post(
                        "/api/v1/agents",
                        headers=bob,
                        json={
                            "name": "Skill Agent",
                            "default_model_id": 202,
                            "skill_ids": [skill_id],
                        },
                    )
                    assert created_agent.status_code == 201, created_agent.text
                    agent = created_agent.json()["data"]
                    assert agent["skill_ids"] == [skill_id]
                    async with sessions.begin() as db:
                        await snapshot_run_skills(db, "run-agent", agent["id"], 102)
                    async with sessions() as db:
                        assert "# 导入版" in (await load_run_skill_files(db, "run-agent", 102))["/skills/shared/SKILL.md"]
                    repinned = await client.put(
                        f"/api/v1/agents/{agent['id']}",
                        headers=bob,
                        json={
                            "expected_version": agent["version"],
                            "skill_ids": [],
                        },
                    )
                    assert repinned.status_code == 200, repinned.text
                    assert repinned.json()["data"]["skill_ids"] == []
                    async with sessions.begin() as db:
                        db.add(RunSkill(run_id="run-public", skill_id=skill_id, skill_version=1))
                    async with sessions() as db:
                        assert "# 步骤" in (await load_run_skill_files(db, "run-public", 102))["/skills/shared/SKILL.md"]
                    disabled = await client.put(
                        f"/api/v1/skills/{skill_id}",
                        headers=alice,
                        json={
                            "expected_version": 3,
                            "enabled": False,
                        },
                    )
                    assert disabled.status_code == 200
                    async with sessions() as db:
                        with pytest.raises(Exception):
                            await load_run_skill_files(db, "run-public", 102)
            finally:
                app.dependency_overrides.pop(get_db_session, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
