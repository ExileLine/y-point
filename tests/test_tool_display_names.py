# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 10:40:40
# @Author  : yangyuexiong
# @File    : test_tool_display_names.py

"""验证工具显示名称与内部标识、MCP 远端函数的独立性。"""

import pytest

from app.schemas.tool import ToolCreateIn, ToolUpdateIn


@pytest.mark.parametrize("kind", ["script", "mcp"])
def test_tool_display_name_accepts_human_readable_names(kind) -> None:
    """界面名称可用中文、大小写及空格，且无需提交内部调用标识。"""
    body = ToolCreateIn.model_validate(
        {
            "display_name": "  天气查询 Weather Tool  ",
            "description": "获取天气",
            "kind": kind,
        }
    )
    assert body.display_name == "天气查询 Weather Tool"
    assert body.name is None
    update = ToolUpdateIn.model_validate(
        {
            "display_name": "新的显示名称",
            "expected_version": 1,
        }
    )
    assert update.display_name == "新的显示名称"


@pytest.mark.parametrize("name", ["   ", "a\nb", "a\tb", "a" * 129])
def test_tool_display_name_rejects_empty_control_and_oversized_names(name) -> None:
    """可读名称仍拒绝纯空白、控制字符及超长值。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ToolCreateIn(display_name=name, kind="script", description="测试")
    with pytest.raises(ValidationError):
        ToolUpdateIn(display_name=name, expected_version=1)


def test_tool_display_name_api_and_stable_model_identity(tmp_path, monkeypatch) -> None:
    """新增和修改显示名均不改写内部标识、远端函数及版本执行定义。"""
    import asyncio
    import re

    import httpx
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.security import get_current_admin
    from app.db.session import get_db_session
    from app.main import app
    from app.models.admin import Admin
    from app.models.base import Base
    from app.models.tool import UserTool, UserToolVersion

    remote_name = "Get-Weather.v2"
    discovered = []

    async def discover(target, token):
        """模拟远端发现，保留原始函数名及服务端只读声明。"""
        discovered.append(target)
        return [
            {
                "name": remote_name,
                "input_schema": {
                    "type": "object",
                },
                "read_only": True,
            }
        ]

    monkeypatch.setattr("app.api.v1.tools.discover_mcp_tools", discover)

    async def exercise():
        """通过真实 ASGI 接口验证名称校验、冲突、目录与旧客户端。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'names.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor = Admin(id=1, username="name-owner", password="unused", role="user")

        async def session_override():
            """提供隔离业务数据库。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_current_admin] = lambda: actor
        app.dependency_overrides[get_db_session] = session_override
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(actor)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                connection = await client.post(
                    "/api/v1/mcp-connections",
                    json={
                        "name": "本机天气",
                        "transport": "streamable_http",
                        "url": "http://127.0.0.1:8000/mcp",
                    },
                )
                assert connection.status_code == 201, connection.text
                draft = {
                    "display_name": "  天气查询 Weather Tool  ",
                    "description": "获取天气",
                    "kind": "mcp",
                    "mcp_connection_id": connection.json()["data"]["id"],
                    "mcp_tool_name": remote_name,
                }
                response = await client.post("/api/v1/tools", json=draft)
                assert response.status_code == 201, response.text
                created = response.json()["data"]
                assert created["display_name"] == "天气查询 Weather Tool"
                assert re.fullmatch(r"[a-z][a-z0-9_]*", created["name"])
                assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", created["model_name"])
                assert created["name"] != created["display_name"]
                assert created["mcp_tool_name"] == remote_name
                assert (await client.post("/api/v1/tools", json=draft)).status_code == 409
                duplicate = await client.post(
                    "/api/v1/tools",
                    json={
                        **draft,
                        "display_name": "另一个显示名称",
                    },
                )
                assert duplicate.status_code == 201
                assert duplicate.json()["data"]["model_name"] != created["model_name"]
                updated = await client.put(
                    f"/api/v1/tools/{created['id']}",
                    json={
                        "expected_version": 1,
                        "display_name": "天气查询 新版 V2",
                    },
                )
                assert updated.status_code == 200, updated.text
                data = updated.json()["data"]
                assert data["display_name"] == "天气查询 新版 V2"
                for key in ("name", "model_name", "mcp_tool_name", "content_hash"):
                    assert data[key] == created[key]
                assert data["current_version"] == 2
                conflict_id = duplicate.json()["data"]["id"]
                conflict = await client.put(
                    f"/api/v1/tools/{conflict_id}",
                    json={
                        "expected_version": 1,
                        "display_name": data["display_name"],
                    },
                )
                assert conflict.status_code == 409, conflict.text
                preserved = (await client.get(f"/api/v1/tools/{conflict_id}")).json()["data"]
                assert preserved["current_version"] == 1
                assert preserved["display_name"] == "另一个显示名称"
                catalog = (await client.get("/api/v1/tools/catalog")).json()["data"]["user_tools"]
                assert next(item for item in catalog if item["id"] == created["id"])["display_name"] == "天气查询 新版 V2"
                legacy = await client.post(
                    "/api/v1/tools",
                    json={
                        "name": "legacy_script",
                        "kind": "script",
                        "description": "旧客户端脚本",
                        "input_schema": {
                            "type": "object",
                        },
                        "script_language": "python",
                        "source_code": "print('{}')",
                    },
                )
                assert legacy.status_code == 201, legacy.text
                assert legacy.json()["data"]["display_name"] == "legacy_script"
                assert legacy.json()["data"]["model_name"].endswith("_legacy_script")
                # 名称可读性与可执行脚本无关，新建脚本也无需手动填写内部标识。
                script = await client.post(
                    "/api/v1/tools",
                    json={
                        "display_name": "中文 Script 工具",
                        "kind": "script",
                        "description": "新脚本",
                        "input_schema": {
                            "type": "object",
                        },
                        "script_language": "python",
                        "source_code": "print('{}')",
                    },
                )
                assert script.status_code == 201, script.text
                assert script.json()["data"]["display_name"] == "中文 Script 工具"
                assert (await client.delete(f"/api/v1/tools/{conflict_id}")).status_code == 200
                recreated = await client.post(
                    "/api/v1/tools",
                    json={
                        **draft,
                        "display_name": "另一个显示名称",
                    },
                )
                assert recreated.status_code == 201, recreated.text
                assert recreated.json()["data"]["id"] != conflict_id
            async with sessions() as db:
                tool = await db.get(UserTool, created["id"])
                versions = (await db.scalars(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id))).all()
                assert len(versions) == 2
                assert {version.mcp_tool_name for version in versions} == {remote_name}
                assert {version.content_hash for version in versions} == {created["content_hash"]}
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(exercise())


def test_tool_display_name_migration_preserves_calling_identity(tmp_path) -> None:
    """真实迁移回填显示名，升级和降级均保持已有内部标识不变。"""
    import os
    import sqlite3
    import subprocess
    import sys

    path = tmp_path / "names-migration.db"
    env = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(path),
    }

    def migrate(*args):
        """在隔离数据库执行真实迁移并保留失败证据。"""
        result = subprocess.run([sys.executable, "-m", "alembic", *args], env=env, text=True, capture_output=True, timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "058")
    with sqlite3.connect(path) as db:
        db.execute("""
            INSERT INTO yp_user_tool (
                owner_id, name, description, kind, visibility, enabled,
                credentials_revoked_through, current_version, create_time,
                update_time, create_timestamp, update_timestamp, is_deleted, status
            ) VALUES (1, 'legacy_reader', '读取', 'mcp', 'private', 1, 0, 1,
                      '2026-11-19 10:40:40', '2026-11-19 10:40:40', 0, 0, 0, 1)
        """)
    migrate("upgrade", "head")
    migrate("check")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name, display_name FROM yp_user_tool").fetchone() == ("legacy_reader", "legacy_reader")
        db.execute("UPDATE yp_user_tool SET display_name = '中文显示名称'")
    migrate("downgrade", "058")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM yp_user_tool").fetchone() == ("legacy_reader",)
        assert "display_name" not in {row[1] for row in db.execute("PRAGMA table_info(yp_user_tool)")}
    migrate("upgrade", "head")
    migrate("check")


def test_skill_dependencies_use_stable_calling_identity(monkeypatch) -> None:
    """Skill 依赖可用模型标识，显示改名不影响依赖，未授权及歧义标识仍拒绝。"""
    import asyncio

    from app.core.custom_exception import CustomException
    from app.models.skill import SkillVersion
    from app.models.tool import UserTool
    from app.services.skill_package_runtime import require_dependencies
    from app.services.user_tools import model_tool_name

    definition = UserTool(id=1, owner_id=1, name="tool_abc", display_name="天气查询")
    required = model_tool_name(definition)
    monkeypatch.setattr(
        "app.services.skill_package_runtime.execution_config",
        lambda files: {
            "required_tools": [required],
        },
    )
    version = SkillVersion(
        files={},
        manifest=[
            {
                "package_file": True,
            }
        ],
    )

    async def exercise():
        """显示名称变更后身份依然匹配，多对象命中仍视为歧义。"""
        await require_dependencies(None, [version], [definition])
        definition.display_name = "天气查询 新版"
        await require_dependencies(None, [version], [definition])
        with pytest.raises(CustomException):
            await require_dependencies(None, [version], [])
        colliding = UserTool(id=2, owner_id=2, name=required)
        with pytest.raises(CustomException):
            await require_dependencies(None, [version], [definition, colliding])

    asyncio.run(exercise())
