# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:17:41
# @Author  : yangyuexiong
# @File    : test_skill_packages.py

"""验证 Skill 草稿隔离、原子发布、资源授权及脚本版本固定。"""

import asyncio
import io
import json
import stat
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import skill_packages as package_api
from app.core.enums import Visibility
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.base import Base
from app.models.skill import AgentSkill, RunSkill, Skill, SkillVersion
from app.models.skill_package import SkillDraft, SkillVersionFile
from app.models.tool import RunTool, UserTool, UserToolVersion
from app.services import skill_packages as packages
from app.services.artifact_store import ArtifactValidationError
from app.services.skill_package_runtime import managed_script_resources, snapshot_skill_scripts
from app.services.skill_reports import template_for_version
from app.services.skills import load_run_skill_files
from tests.test_skill_reports import SCHEMA, template_bytes
from tests.test_skills import skill_text


@asynccontextmanager
async def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """在临时业务库及私有目录执行真实 HTTP 契约，不接触用户数据。"""
    root = tmp_path / "resources"
    root.mkdir()
    monkeypatch.setattr(packages, "artifact_root", lambda: root)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'packages.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as db:
        db.add_all([Admin(id=1, username="owner", password="test-only"), Admin(id=2, username="other", password="test-only")])

    async def database():
        """逐请求提供独立事务。"""
        async with sessions() as db:
            yield db

    async def actor():
        """使用隔离库的固定操作者。"""
        async with sessions() as db:
            return await db.get(Admin, 1)

    app.dependency_overrides[get_db_session] = database
    app.dependency_overrides[get_current_admin] = actor
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client, sessions, root
    finally:
        app.dependency_overrides.pop(get_db_session, None)
        app.dependency_overrides.pop(get_current_admin, None)
        await engine.dispose()


async def create(client: httpx.AsyncClient, name: str = "review-package") -> int:
    """创建私有未发布工作区。"""
    response = await client.post(
        "/api/v1/skills/packages",
        json={
            "name": name,
            "description": "审查资料",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["current_version"] == 0
    return response.json()["data"]["id"]


def test_workspace_publish_isolation_and_zip_roundtrip(tmp_path, monkeypatch) -> None:
    """完整往返保留二进制和路径，草稿修订不改变 Agent 已固定的正文及模板。"""

    async def exercise():
        """覆盖发布、旧版本、撤权、过期编辑与完整文件导出。"""
        async with isolated(tmp_path, monkeypatch) as (client, sessions, root):
            identifier = await create(client)
            prefix = f"/api/v1/skills/{identifier}"
            config = "templates:\n  assets/templates/report.docx: assets/schemas/report.json\n"
            save = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 1,
                    "files": {
                        "references/policy.md": "原始准则",
                        "assets/schemas/report.json": json.dumps(SCHEMA),
                        "y-point.yaml": config,
                    },
                },
            )
            assert save.status_code == 200, save.text
            raw = template_bytes()
            uploaded = await client.post(
                prefix + "/draft/files",
                data={
                    "expected_revision": "2",
                    "paths": json.dumps(["assets/templates/report.docx"]),
                },
                files=[("files", ("report.docx", raw))],
            )
            assert uploaded.status_code == 200, uploaded.text
            checked = await client.post(
                prefix + "/draft/validate",
                json={
                    "expected_revision": 3,
                },
            )
            assert checked.json()["data"]["valid"], checked.text
            published = await client.post(
                prefix + "/draft/publish",
                json={
                    "expected_revision": 3,
                },
            )
            assert published.status_code == 201, published.text
            async with sessions.begin() as db:
                assert len(list(await db.scalars(select(SkillVersionFile)))) == 5
                db.add(RunSkill(run_id="old-run", skill_id=identifier, skill_version=1))
                fixed = await template_for_version(db, identifier, 1, "assets/templates/report.docx")
                assert fixed is not None
                assert (await template_for_version(db, identifier, 1, "report.docx")).id == fixed.id
            response = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 4,
                    "files": {
                        "references/policy.md": "新准则",
                    },
                },
            )
            assert response.status_code == 200, response.text
            stale = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 4,
                    "files": {
                        "README.md": "旧标签页",
                    },
                },
            )
            assert stale.status_code == 409
            async with sessions() as db:
                virtual = await load_run_skill_files(db, "old-run", 1)
                assert virtual["/skills/review-package/references/policy.md"] == "原始准则"
            downloaded = await client.get(prefix + "/versions/1/export")
            assert downloaded.status_code == 200, downloaded.text
            with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
                assert archive.read("review-package/assets/templates/report.docx") == raw
                assert archive.read("review-package/references/policy.md").decode() == "原始准则"
            assert packages.unpack_package(downloaded.content)["assets/templates/report.docx"] == raw
            legacy = await client.put(
                prefix,
                json={
                    "expected_version": 1,
                    "enabled": False,
                },
            )
            assert legacy.status_code == 409
            before = {path.name for path in root.iterdir()}
            invalid = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 5,
                    "files": {
                        "README.md": "暂存",
                    },
                    "delete_paths": ["SKILL.md"],
                },
            )
            assert invalid.status_code == 400
            assert before == {path.name for path in root.iterdir()}
            # 公开只暴露当前发布文件，草稿与历史权限仍由所有者控制。
            response = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 5,
                    "visibility": "public",
                },
            )
            assert response.status_code == 200
            assert (
                await client.post(
                    prefix + "/draft/publish",
                    json={
                        "expected_revision": 6,
                    },
                )
            ).status_code == 201

            async def other():
                """切换至没有草稿权限的用户。"""
                async with sessions() as db:
                    return await db.get(Admin, 2)

            app.dependency_overrides[get_current_admin] = other
            workspace = (await client.get(prefix + "/workspace")).json()["data"]
            assert workspace["readonly"] and workspace["draft"] is None and len(workspace["versions"]) == 1
            assert (
                await client.get(
                    prefix + "/files",
                    params={
                        "path": "references/policy.md",
                    },
                )
            ).status_code == 404
            assert (
                await client.get(
                    prefix + "/files",
                    params={
                        "path": "references/policy.md",
                        "version": 1,
                    },
                )
            ).status_code == 404
            current = await client.get(
                prefix + "/files",
                params={
                    "path": "references/policy.md",
                    "version": 2,
                },
            )
            assert current.status_code == 200 and current.text == "新准则"
            async with sessions.begin() as db:
                (await db.get(Skill, identifier)).visibility = Visibility.PRIVATE
            assert (await client.get(prefix + "/versions/2/export")).status_code == 404

    asyncio.run(exercise())


def test_script_verification_uses_whole_package_and_fixed_run(tmp_path, monkeypatch) -> None:
    """脚本进入现有 Tool 审计链，所有资源变化使验证失效，旧 Run 保留原始资源。"""

    async def exercise():
        """以受控沙箱返回值检验依赖准备、试运行与运行装载的所有绑定。"""
        async with isolated(tmp_path, monkeypatch) as (client, sessions, _):
            calls = []

            async def sandbox(language, source, arguments, timeout, **options):
                """仅捕获传入沙箱的固定文件，不启动真实模型或外部工具。"""
                calls.append(options)
                return '{"valid":true}'

            monkeypatch.setattr(package_api, "run_script", sandbox)
            identifier = await create(client)
            prefix = f"/api/v1/skills/{identifier}"
            response = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 1,
                    "files": {
                        "scripts/validate.py": "import json\nprint(json.dumps(True))\n",
                        "references/policy.md": "v1",
                        "y-point.yaml": "scripts:\n  scripts/validate.py:\n    input_schema:\n      type: object\n      properties: {}\n",
                    },
                },
            )
            assert response.status_code == 200, response.text
            body = {
                "expected_revision": 2,
                "path": "scripts/validate.py",
                "arguments": {},
            }
            assert (
                await client.post(
                    prefix + "/draft/publish",
                    json={
                        "expected_revision": 2,
                    },
                )
            ).status_code == 409
            prepared = await client.post(prefix + "/draft/scripts/prepare", json=body)
            assert prepared.status_code == 200, prepared.text
            tested = await client.post(prefix + "/draft/scripts/test", json=body)
            assert tested.status_code == 200, tested.text
            assert calls[0]["package_files"]["references/policy.md"] == b"v1"
            assert (
                await client.post(
                    prefix + "/draft/publish",
                    json={
                        "expected_revision": 2,
                    },
                )
            ).status_code == 201
            async with sessions.begin() as db:
                row = AgentSkill(agent_id=99, skill_id=identifier, skill_version=1)
                await snapshot_skill_scripts(db, "script-run", 99, 1, [row])
                db.add(RunSkill(run_id="script-run", skill_id=identifier, skill_version=1))
            async with sessions() as db:
                tool = await db.scalar(select(UserTool))
                version = await db.scalar(select(UserToolVersion))
                resources = await managed_script_resources(db, "script-run", 1, tool, version)
                assert resources["entrypoint"] == "scripts/validate.py"
                assert resources["package_files"]["references/policy.md"] == b"v1"
                assert (await db.scalar(select(RunTool))).content_hash == version.content_hash
                tool_id = tool.id
            catalog = await client.get("/api/v1/tools/catalog")
            assert not any(item["id"] == tool_id for item in catalog.json()["data"]["user_tools"])
            assert (await client.get(f"/api/v1/tools/{tool_id}")).status_code == 404
            assert (
                await client.put(
                    f"/api/v1/tools/{tool_id}",
                    json={
                        "expected_version": 1,
                        "enabled": False,
                    },
                )
            ).status_code == 404
            assert (
                await client.put(
                    prefix + "/draft",
                    json={
                        "expected_revision": 3,
                        "files": {
                            "references/policy.md": "v2",
                        },
                    },
                )
            ).status_code == 200
            checked = (
                await client.post(
                    prefix + "/draft/validate",
                    json={
                        "expected_revision": 4,
                    },
                )
            ).json()["data"]
            assert not checked["valid"] and any("脚本" in error for error in checked["errors"])

    asyncio.run(exercise())


def test_quota_failure_rolls_back_files_and_draft(tmp_path, monkeypatch) -> None:
    """配额失败按业务 400 返回且不留下私有文件和半成品草稿。"""

    async def exercise():
        """模拟确定的容量错误，核对清理和事务。"""
        async with isolated(tmp_path, monkeypatch) as (client, sessions, root):

            async def quota(*args, **kwargs):
                """提供确定的配额拒绝。"""
                raise ArtifactValidationError("文件数量或存储容量超过用户配额")

            monkeypatch.setattr(packages, "enforce_owner_quota", quota)
            response = await client.post(
                "/api/v1/skills/packages",
                json={
                    "name": "limited",
                    "description": "测试",
                },
            )
            assert response.status_code == 400, response.text
            assert not list(root.iterdir())
            async with sessions() as db:
                assert await db.scalar(select(SkillDraft)) is None
                assert await db.scalar(select(Skill)) is None

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["../bad", "/tmp/bad", "references/../bad", "a//b", "a\\b", ".secret", "assets/C:bad", "x\x00"])
def test_package_path_rejects_escape(path) -> None:
    """逻辑路径不接受目录逃逸或跨平台歧义。"""
    with pytest.raises(ValueError):
        packages.package_path(path)


def test_zip_and_execution_configuration_reject_unsafe_shapes() -> None:
    """拒绝链接、大小写碰撞、目录冲突及别名或非对象脚本配置。"""
    for paths in [("SKILL.md", "skill.md"), ("SKILL.md", "references", "references/a.md")]:
        with pytest.raises(ValueError):
            packages.check_entries(
                {
                    path: {
                        "bytes": 1,
                    }
                    for path in paths
                },
            )
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("SKILL.md", skill_text("safe"))
        info = zipfile.ZipInfo("scripts/link.py")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(ValueError):
        packages.unpack_package(data.getvalue())
    for config in (
        "scripts: []",
        "scripts: {scripts/check.py: []}",
        "templates: {assets/templates/a.docx: []}",
        "scripts: &s {scripts/check.py: *s}",
    ):
        with pytest.raises(ValueError):
            packages.execution_config(
                {
                    "y-point.yaml": config,
                },
            )


def test_skill_upload_limit_precedes_multipart_parsing() -> None:
    """声明长度与无长度分块超限均在模板或文件包 multipart 解析前拒绝。"""
    from app.core.middleware import ArtifactUploadLimitMiddleware

    async def exercise():
        """直接驱动 ASGI 接收流验证每类上传的实际限额。"""
        for path, limit in (
            ("/api/v1/skills/1/templates", 21),
            ("/api/v1/skills/1/template", 21),
            ("/api/v1/skills/packages/import", 33),
            ("/api/v1/skills/1/draft/files", 33),
        ):
            for declared in (True, False):
                events = []
                invoked = False

                async def downstream(scope, receive, send):
                    """模拟 multipart 解析读取正文，只在未超限时响应。"""
                    nonlocal invoked
                    invoked = True
                    await receive()
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 200,
                            "headers": [],
                        },
                    )
                    await send(
                        {
                            "type": "http.response.body",
                            "body": b"ok",
                        },
                    )

                async def receive():
                    """提供确定超限的请求分块。"""
                    return {
                        "type": "http.request",
                        "body": b"x" * (limit * 1024 * 1024 + 1),
                        "more_body": False,
                    }

                async def send(event):
                    """捕获最终 ASGI 响应。"""
                    events.append(event)

                scope = {
                    "type": "http",
                    "path": path,
                    "method": "POST",
                    "headers": [(b"content-length", str(limit * 1024 * 1024 + 1).encode())] if declared else [],
                }
                await ArtifactUploadLimitMiddleware(downstream)(scope, receive, send)
                assert events[0]["status"] == 413
                assert invoked is not declared

    asyncio.run(exercise())


def test_legacy_resources_convert_only_in_draft_and_quota_failures_are_business_errors(tmp_path, monkeypatch) -> None:
    """旧模板转到逻辑目录时历史版本不变，独立模板路由的配额失败也返回 400。"""
    from app.api.v1 import skill_templates

    async def exercise():
        """检验旧接口与新工作区之间的兼容性及模板上传清理。"""
        async with isolated(tmp_path, monkeypatch) as (client, sessions, root):
            monkeypatch.setattr(skill_templates, "artifact_root", lambda: root)
            from app.services import skill_reports

            monkeypatch.setattr(skill_reports, "artifact_root", lambda: root)
            created = await client.post(
                "/api/v1/skills",
                json={
                    "files": {
                        "SKILL.md": skill_text("legacy"),
                    },
                    "visibility": "private",
                },
            )
            identifier = created.json()["data"]["id"]
            prefix = f"/api/v1/skills/{identifier}"

            async def quota(*args, **kwargs):
                """拒绝模板配额且保留原始错误。"""
                raise ArtifactValidationError("文件数量或存储容量超过用户配额")

            with monkeypatch.context() as patch:
                patch.setattr(skill_templates, "enforce_owner_quota", quota)
                response = await client.post(
                    prefix + "/templates",
                    data={
                        "expected_version": "1",
                        "fields_schema": json.dumps(
                            {
                                "old.docx": SCHEMA,
                            },
                        ),
                    },
                    files=[("files", ("old.docx", template_bytes()))],
                )
                assert response.status_code == 400, response.text
                assert not list(root.iterdir())
            response = await client.post(
                prefix + "/templates",
                data={
                    "expected_version": "1",
                    "fields_schema": json.dumps(
                        {
                            "old.docx": SCHEMA,
                        },
                    ),
                },
                files=[("files", ("old.docx", template_bytes()))],
            )
            assert response.status_code == 201, response.text
            draft = await client.post(prefix + "/draft", json={})
            assert draft.status_code == 200, draft.text
            assert "assets/templates/old.docx" in {entry["path"] for entry in draft.json()["data"]["files"]}
            checked = await client.post(
                prefix + "/draft/validate",
                json={
                    "expected_revision": 1,
                },
            )
            assert checked.json()["data"]["valid"], checked.text
            preview = await client.get(
                prefix + "/files/preview",
                params={
                    "path": "assets/templates/old.docx",
                },
            )
            assert preview.status_code == 200 and preview.json()["data"]["kind"] == "html", preview.text
            async with sessions() as db:
                legacy = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == identifier, SkillVersion.version == 2))
                assert not any(entry.get("package_file") for entry in legacy.manifest)

    asyncio.run(exercise())


def test_published_package_script_executes_in_real_sandbox_and_records_calls(tmp_path, monkeypatch) -> None:
    """受控模型在真实 Docker 沙箱读取固定包资料并调用脚本，留下调用与版本证据。"""
    from langchain_core.messages import AIMessage
    from app.models.agent_config import AgentConfig
    from app.models.tool import ToolInvocation
    from app.services.run_calls import load_run_calls
    from app.services.run_executor import execute_text_run
    from app.services.run_manager import create_run
    from app.services.skills import snapshot_run_skills
    from app.services.worker_tools import make_worker_tool_resolver
    from app.services.user_tools import model_tool_name
    from tests.test_skills import SkillModel

    async def exercise():
        """运行完整图、包内相对导入与资源读取，不调用外部模型或网络服务。"""
        async with isolated(tmp_path, monkeypatch) as (client, sessions, _):
            identifier = await create(client)
            prefix = f"/api/v1/skills/{identifier}"
            source = (
                "import json, sys\nfrom helper import multiply\n"
                "with open('references/factor.txt') as f:\n    factor = int(f.read())\n"
                "data = json.load(sys.stdin)\nprint(json.dumps({'result': multiply(data['value'], factor)}))\n"
            )
            response = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 1,
                    "files": {
                        "scripts/check.py": source,
                        "scripts/helper.py": 'def multiply(value, factor):\n    """按固定因子计算结果。"""\n    return value * factor\n',
                        "references/factor.txt": "2",
                        "y-point.yaml": (
                            "scripts:\n  scripts/check.py:\n    input_schema:\n      type: object\n"
                            "      properties:\n        value:\n          type: integer\n      required: [value]\n"
                        ),
                    },
                },
            )
            assert response.status_code == 200, response.text
            tested = await client.post(
                prefix + "/draft/scripts/test",
                json={
                    "expected_revision": 2,
                    "path": "scripts/check.py",
                    "arguments": {
                        "value": 21,
                    },
                },
            )
            assert tested.status_code == 200, tested.text
            assert json.loads(tested.json()["data"]["result"])["result"] == 42
            assert (
                await client.post(
                    prefix + "/draft/publish",
                    json={
                        "expected_revision": 2,
                    },
                )
            ).status_code == 201
            assert (
                await client.put(
                    prefix + "/draft",
                    json={
                        "expected_revision": 3,
                        "files": {
                            "references/factor.txt": "3",
                        },
                    },
                )
            ).status_code == 200
            async with sessions.begin() as db:
                db.add(AgentConfig(id=99, owner_id=1, name="Script Agent", default_model_id=1, enabled=True, version=1))
                db.add(AgentSkill(agent_id=99, skill_id=identifier, skill_version=1))
                await db.flush()
                run = await create_run(db, 1, agent_id=99, input_text="调用包内脚本计算21乘2")
                await snapshot_run_skills(db, run.id, 99, 1)
                tool = await db.scalar(select(UserTool).where(UserTool.skill_id == identifier))
                name = model_tool_name(tool)
            model = SkillModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {
                                    "file_path": "/skills/review-package/SKILL.md",
                                },
                                "id": "read-skill",
                            },
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": name,
                                "args": {
                                    "value": 21,
                                },
                                "id": "call-script",
                            },
                        ],
                    ),
                    AIMessage(content="固定包结果为42"),
                ],
            )
            from app.services import worker_tools

            runtime_results = []
            sandbox = worker_tools.run_script

            async def record_sandbox(*args, **kwargs):
                """保持真实沙箱执行并记录本轮脚本结果，核对旧包资料仍为原值。"""
                response = await sandbox(*args, **kwargs)
                runtime_results.append(json.loads(response))
                return response

            monkeypatch.setattr(worker_tools, "run_script", record_sandbox)
            result = await execute_text_run(
                sessions,
                run.id,
                model,
                tmp_path / "checkpoints.db",
                resolve_tools=make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=1,
            )
            assert result.state == "completed", result.failure_code
            assert runtime_results == [
                {
                    "result": 42,
                },
            ]
            async with sessions() as db:
                invocation = await db.scalar(select(ToolInvocation).where(ToolInvocation.run_id == run.id))
                assert invocation.state == "completed"
                calls = await load_run_calls(db, 1, [run.id])
                assert any(item.name == "review-package@v1 / scripts/check.py" and item.status == "succeeded" for item in calls[run.id])

    asyncio.run(exercise())


def test_required_tools_are_explicit_and_unambiguous() -> None:
    """声明工具不会授权新能力，缺失与重名都阻止关联。"""
    from app.core.custom_exception import CustomException
    from app.services.skill_package_runtime import require_dependencies

    async def exercise():
        """直接检查发布包与可授权工具集合的匹配关系。"""
        version = SkillVersion(
            files={
                "y-point.yaml": "required_tools: [func1]\n",
            },
            manifest=[
                {
                    "package_file": True,
                },
            ],
        )
        for tools in ([], [UserTool(name="func1"), UserTool(name="func1")]):
            with pytest.raises(CustomException):
                await require_dependencies(None, [version], tools)
        await require_dependencies(None, [version], [UserTool(name="func1")])

    asyncio.run(exercise())


def test_invalid_template_syntax_is_a_publish_blocker_not_server_error(tmp_path, monkeypatch) -> None:
    """损坏的模板语法保留在草稿中供修正，校验与发布给出明确问题。"""

    async def exercise():
        """通过实际上传与校验路径验证未闭合模板标记的失败契约。"""
        async with isolated(tmp_path, monkeypatch) as (client, sessions, _):
            identifier = await create(client)
            prefix = f"/api/v1/skills/{identifier}"
            response = await client.put(
                prefix + "/draft",
                json={
                    "expected_revision": 1,
                    "files": {
                        "y-point.yaml": "templates:\n  assets/templates/bad.docx: assets/schemas/report.json\n",
                        "assets/schemas/report.json": json.dumps(SCHEMA),
                    },
                },
            )
            assert response.status_code == 200
            response = await client.post(
                prefix + "/draft/files",
                data={
                    "expected_revision": "2",
                    "paths": '["assets/templates/bad.docx"]',
                },
                files=[("files", ("bad.docx", template_bytes("{{ title")))],
            )
            assert response.status_code == 200, response.text
            checked = await client.post(
                prefix + "/draft/validate",
                json={
                    "expected_revision": 3,
                },
            )
            assert checked.status_code == 200 and not checked.json()["data"]["valid"], checked.text
            assert (
                await client.post(
                    prefix + "/draft/publish",
                    json={
                        "expected_revision": 3,
                    },
                )
            ).status_code == 409
            async with sessions() as db:
                assert await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == identifier)) is None

    asyncio.run(exercise())
