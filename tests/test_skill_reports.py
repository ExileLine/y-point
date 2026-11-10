# -*- coding: utf-8 -*-
# @Time    : 2026-11-05 10:11:04
# @Author  : yangyuexiong
# @File    : test_skill_reports.py

"""验证模板版本固定、审查附件输入、报告交付契约及私有下载链路。"""

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from docx import Document
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.password import hash_password
from app.core.enums import ArtifactOrigin, ArtifactState, ModelCapability, ProviderKind, RunEventType, UserRole
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.agent_run_event import AgentRunEvent
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.skill import RunSkill
from app.models.skill_template import SkillTemplate
from app.services import skill_reports, message_attachments, worker_tools
from app.api.v1 import artifacts as artifact_api, skill_templates as template_api
from app.services.skills import load_run_skill_files
from app.services.run_executor import execute_text_run
from app.services.worker_tools import make_worker_tool_resolver
from tests.test_skills import SkillModel, skill_text

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "maxLength": 100,
        },
        "summary": {
            "type": "string",
            "maxLength": 200,
        },
        "findings": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "maxLength": 128,
                    },
                },
                "required": ["note"],
            },
        },
    },
    "required": ["title", "summary", "findings"],
}


def template_bytes(title: str = "{{ title }}") -> bytes:
    """生成含页眉、正文和循环表格的真正 Word 模板。"""
    document = Document()
    document.sections[0].header.paragraphs[0].text = "{{ title }}"
    document.add_heading(title, 0)
    document.add_paragraph("{{ summary }}")
    table = document.add_table(rows=4, cols=1)
    table.style = "Table Grid"
    for row, text in zip(table.rows, ("发现项", "{%tr for item in findings %}", "{{ item.note }}", "{%tr endfor %}")):
        row.cells[0].text = text
    stream = io.BytesIO()
    document.save(stream)
    return stream.getvalue()


def test_template_rejects_executable_or_unbounded_content() -> None:
    """只允许声明字段与有限列表循环，拒绝代码、未知变量及无限放大。"""
    schema = skill_reports.validate_fields_schema(SCHEMA)
    skill_reports.validate_docx_template(template_bytes(), schema)
    for malicious in (
        "{{ title.upper() }}",
        "{{ unknown }}",
        "{% for c in title %}{{ c }}{% endfor %}",
        "{% for a in findings %}{% for b in findings %}{{ b.note }}{% endfor %}{% endfor %}",
    ):
        with pytest.raises(ValueError):
            skill_reports.validate_docx_template(template_bytes(malicious), schema)
    with pytest.raises(ValueError):
        skill_reports.validate_fields_schema(
            {
                "type": "object",
                "$ref": "https://example.com/schema",
            },
        )


def test_template_review_http_worker_download_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """使用隔离业务库和受控模型完成上传至报告下载，并验证撤权、版本与幂等。"""

    async def exercise() -> None:
        """执行完整 HTTP、数据库、worker 工具和产物下载链路。"""
        root = tmp_path / "artifacts"
        root.mkdir(mode=0o700)
        for module in (artifact_api, template_api, skill_reports, message_attachments, worker_tools):
            monkeypatch.setattr(module, "artifact_root", lambda: root)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reports.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        Admin(
                            id=1,
                            username="reviewer",
                            password=hash_password("StrongPassword123"),
                            role=UserRole.USER,
                            status=1,
                            is_deleted=0,
                        ),
                        Admin(
                            id=2,
                            username="outsider",
                            password=hash_password("StrongPassword123"),
                            role=UserRole.USER,
                            status=1,
                            is_deleted=0,
                        ),
                        ProviderConnection(
                            id=1,
                            name="reports",
                            name_key="reports",
                            kind=ProviderKind.OLLAMA,
                            base_url="http://localhost:11434/v1",
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                        ModelCatalog(
                            id=1,
                            provider_id=1,
                            external_id="test-only",
                            display_name="Test model",
                            capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                            verified_capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                            capabilities_verified=True,
                            context_window=32768,
                            creator_id=1,
                            status=1,
                            is_deleted=0,
                        ),
                    ],
                )

            async def session_override():
                """将所有请求隔离至临时数据库。"""
                async with sessions() as db:
                    yield db

            app.dependency_overrides[get_db_session] = session_override
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

                async def login(name: str) -> dict:
                    """通过真实登录接口获取隔离用户凭据。"""
                    result = await client.post(
                        "/api/v1/auth/login",
                        json={
                            "identifier": name,
                            "password": "StrongPassword123",
                        },
                    )
                    assert result.status_code == 200, result.text
                    return {
                        "Authorization": f"Bearer {result.json()['data']['access_token']}",
                    }

                owner, other = await login("reviewer"), await login("outsider")
                created = await client.post(
                    "/api/v1/skills",
                    headers=owner,
                    json={
                        "files": {
                            "SKILL.md": skill_text("contract-review", "审查用户合同；未上传时调用 func1 获取数据，按照 xxx_template.docx 生成报告。"),
                        },
                    },
                )
                assert created.status_code == 201, created.text
                skill_id = created.json()["data"]["id"]
                endpoint = f"/api/v1/skills/{skill_id}/template"
                payload = {
                    "expected_version": "1",
                    "fields_schema": json.dumps(SCHEMA),
                }
                binary = {
                    "file": (
                        "xxx_template.docx",
                        template_bytes(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    ),
                }
                assert (await client.post(endpoint, headers=other, data=payload, files=binary)).status_code == 404
                invalid = await client.post(
                    endpoint,
                    headers=owner,
                    data=payload,
                    files={
                        "file": ("bad.docx", b"invalid docx", "application/octet-stream"),
                    },
                )
                assert invalid.status_code == 400 and list(root.iterdir()) == []
                published = await client.post(endpoint, headers=owner, data=payload, files=binary)
                assert published.status_code == 201, published.text
                resource = published.json()["data"]["report_template"]["artifact_id"]
                assert published.json()["data"]["current_version"] == 2
                assert (await client.post(endpoint, headers=owner, data=payload, files=binary)).status_code == 409
                assert (await client.delete(f"/api/v1/artifacts/{resource}", headers=owner)).status_code == 409
                assert (await client.get(f"/api/v1/artifacts/{resource}/download", headers=other)).status_code == 404
                created = await client.post(
                    "/api/v1/agents",
                    headers=owner,
                    json={
                        "name": "合同审查助手",
                        "default_model_id": 1,
                        "skill_ids": [skill_id],
                    },
                )
                assert created.status_code == 201, created.text
                agent = created.json()["data"]
                path = f"/api/v1/agents/{agent['id']}/sessions"
                created = await client.post(
                    path,
                    headers=owner,
                    json={
                        "title": "审查场景",
                    },
                )
                assert created.status_code == 201, created.text
                session = created.json()["data"]
                path += "/" + session["id"]
                updated = await client.put(
                    f"/api/v1/skills/{skill_id}",
                    headers=owner,
                    json={
                        "expected_version": 2,
                        "files": {
                            "SKILL.md": skill_text("contract-review", "新版本审查说明"),
                        },
                    },
                )
                assert updated.status_code == 200, updated.text
                assert updated.json()["data"]["report_template"]["artifact_id"] == resource
                newer = await client.post(
                    endpoint,
                    headers=owner,
                    data={
                        "expected_version": "3",
                        "fields_schema": json.dumps(SCHEMA),
                    },
                    files={
                        "file": ("新版报告.docx", template_bytes("新版 {{ title }}"), "application/octet-stream"),
                    },
                )
                assert newer.status_code == 201 and newer.json()["data"]["current_version"] == 4
                assert (await client.get(path + "/report-skills", headers=owner)).status_code == 404
                uploaded = await client.post(
                    "/api/v1/artifacts/uploads",
                    headers=owner,
                    files=[
                        ("files", ("待审合同.txt", "费用在 15 个工作日内报销。".encode(), "text/plain")),
                    ],
                )
                assert uploaded.status_code == 201, uploaded.text
                file_id = uploaded.json()["data"][0]["id"]
                selection = {
                    "artifact_ids": [file_id],
                    "context_artifact_ids": [file_id],
                }
                check = await client.post(path + "/attachments/check", headers=owner, json=selection)
                assert check.status_code == 200, check.text
                assert check.json()["data"]["context_bytes"] > 0
                wrong = await client.post(path + "/attachments/check", headers=other, json=selection)
                assert wrong.status_code == 404
                oversized = await client.post(
                    "/api/v1/artifacts/uploads",
                    headers=owner,
                    files=[
                        ("files", ("too-long.txt", b"x" * (20 * 1024 * 1024), "text/plain")),
                    ],
                )
                long_id = oversized.json()["data"][0]["id"]
                rejected = await client.post(
                    path + "/attachments/check",
                    headers=owner,
                    json={
                        "artifact_ids": [long_id, file_id],
                        "context_artifact_ids": [long_id, file_id],
                    },
                )
                assert rejected.status_code == 413
                ordinary = await client.post(
                    path + "/runs",
                    headers=owner,
                    json={
                        "input_text": "你好",
                        "request_key": "ordinary-chat",
                    },
                )
                assert ordinary.status_code == 201, ordinary.text
                normal = await execute_text_run(
                    sessions,
                    ordinary.json()["data"]["id"],
                    checkpoint_path=tmp_path / "checkpoints.db",
                    model=SkillModel(responses=[AIMessage(content="你好，有什么可以帮你？")]),
                    resolve_tools=make_worker_tool_resolver(sessions),
                    lease_seconds=10,
                    poll_seconds=0.2,
                )
                assert normal.state == "completed", normal.failure_code
                draft = {
                    "input_text": "请审查合同",
                    "request_key": "review-one",
                    **selection,
                }
                submitted = await client.post(path + "/runs", headers=owner, json=draft)
                assert submitted.status_code == 201, submitted.text
                run_id = submitted.json()["data"]["id"]
                assert (await client.post(path + "/runs", headers=owner, json=draft)).json()["data"]["id"] == run_id
                mismatch = await client.post(
                    path + "/runs",
                    headers=owner,
                    json={
                        **draft,
                        "input_text": "改变审查任务",
                    },
                )
                assert mismatch.status_code == 409
                async with sessions() as db:
                    fixed = await db.scalar(select(RunSkill).where(RunSkill.run_id == run_id))
                    assert fixed.skill_version == 2 and not fixed.required_report
                    skill_files = await load_run_skill_files(db, run_id, 1)
                    instruction = skill_files["/skills/contract-review/SKILL.md"]
                    assert "xxx_template.docx" in instruction and "func1" in instruction
                    assert "render_skill_document" in instruction and "JSON Schema" in instruction
                    assert "新版报告.docx" not in instruction
                    assert await db.scalar(select(SkillTemplate.id).where(SkillTemplate.skill_version == 3)) is not None
                    source = await db.get(Artifact, resource)
                    assert source.expires_at is None and source.source == "skill_resource"
                data = {
                    "title": "合同审查报告",
                    "summary": "条款规定 15 个工作日内报销。原文含 {{ expense_limit }} 标记。",
                    "findings": [
                        {
                            "note": "缺少逾期处理约定 <待补充> & 核实",
                        },
                    ],
                }
                model = SkillModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {
                                        "file_path": "/skills/contract-review/SKILL.md",
                                    },
                                    "id": "read-review-skill",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "render_skill_document",
                                    "args": {
                                        "skill_id": skill_id,
                                        "report_data": data,
                                        "filename": "合同审查报告.docx",
                                    },
                                    "id": "render-review",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "render_skill_document",
                                    "args": {
                                        "skill_id": skill_id,
                                        "report_data": data,
                                        "filename": "合同审查报告.docx",
                                    },
                                    "id": "render-review-retry",
                                },
                            ],
                        ),
                        AIMessage(content="已完成审查并生成报告。"),
                    ],
                )
                result = await execute_text_run(
                    sessions,
                    run_id,
                    checkpoint_path=tmp_path / "checkpoints.db",
                    model=model,
                    resolve_tools=make_worker_tool_resolver(sessions),
                    lease_seconds=10,
                    poll_seconds=0.2,
                )
                assert result.state == "completed", result.failure_code
                async with sessions() as db:
                    fixed = await db.scalar(select(RunSkill).where(RunSkill.run_id == run_id))
                    assert fixed.required_report
                    files = await skill_reports.required_report_skill_ids(db, run_id)
                    assert files == [skill_id]
                    events = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == run_id))).all()
                    assert "".join(event.content or "" for event in events if event.event_type == RunEventType.TEXT_DELTA) == "已完成审查并生成报告。"
                assert (await client.post(path + "/runs", headers=owner, json=draft)).json()["data"]["id"] == run_id
                response = await client.get(path + "/messages", headers=owner)
                assert response.status_code == 200, response.text
                messages = [item for item in response.json()["data"] if item["run_id"] == run_id]
                assert messages[0]["attachments"][0]["artifact_id"] == file_id
                assert len(messages[-1]["artifacts"]) == 1
                report = messages[-1]["artifacts"][0]
                provenance = report["template_source"]
                assert provenance["skill_version"] == 2 and provenance["template_artifact_id"] == resource
                download = await client.get(f"/api/v1/artifacts/{report['artifact_id']}/download", headers=owner)
                assert download.status_code == 200, download.text
                document = Document(io.BytesIO(download.content))
                assert document.paragraphs[0].text == data["title"]
                assert document.paragraphs[1].text == data["summary"]
                assert document.sections[0].header.paragraphs[0].text == data["title"]
                assert document.tables[0].rows[1].cells[0].text == data["findings"][0]["note"]
                assert document.tables[0].style.name == "Table Grid"
                assert (await client.get(f"/api/v1/artifacts/{report['artifact_id']}/preview", headers=owner)).status_code == 200
                assert (await client.get(f"/api/v1/artifacts/{report['artifact_id']}/download", headers=other)).status_code == 404
                # 任意通用 DOCX 不能替代固定模板交付。
                next_draft = {
                    **draft,
                    "request_key": "wrong-report",
                }
                submitted = await client.post(path + "/runs", headers=owner, json=next_draft)
                assert submitted.status_code == 201, submitted.text
                run2 = submitted.json()["data"]["id"]
                generic = SkillModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {
                                        "file_path": "/skills/contract-review/SKILL.md",
                                    },
                                    "id": "activate-report-skill",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "create_document_artifact",
                                    "args": {
                                        "filename": "普通报告.docx",
                                        "content": "没有使用固定模板",
                                    },
                                    "id": "wrong-template",
                                },
                            ],
                        ),
                        AIMessage(content="完成"),
                        AIMessage(content="无法按模板交付"),
                    ],
                )
                failed = await execute_text_run(
                    sessions,
                    run2,
                    checkpoint_path=tmp_path / "checkpoints.db",
                    model=generic,
                    resolve_tools=make_worker_tool_resolver(sessions),
                    lease_seconds=10,
                    poll_seconds=0.2,
                )
                assert failed.state == "failed"
                # 已生成通用 DOCX 仍不能满足固定模板的交付约束。
                assert failed.failure_code == "artifact_required"
                async with sessions() as db:
                    text_events = await db.scalars(
                        select(AgentRunEvent).where(AgentRunEvent.run_id == run2, AgentRunEvent.event_type == RunEventType.TEXT_DELTA),
                    )
                    assert list(text_events) == []
                # 模板数据也可来自正文指定的工具，不强制上传附件或指定报告选项。
                from_tool = await client.post(
                    path + "/runs",
                    headers=owner,
                    json={
                        "input_text": "请获取数据并出具审查报告",
                        "request_key": "tool-only-report",
                    },
                )
                assert from_tool.status_code == 201, from_tool.text
                tool_run = from_tool.json()["data"]["id"]
                tool_calls = []

                @tool
                async def func1() -> dict:
                    """返回隔离测试中的业务数据，模拟 Skill 正文指定的取数工具。"""
                    tool_calls.append("func1")
                    return {
                        "title": "工具数据审查报告",
                        "summary": "工具取数：合同编号 TEST-001，付款期限 15 天。",
                        "findings": [],
                    }

                async def tools_with_func1(db, run):
                    """保留真实产物工具，并注册无需外部依赖的只读取数工具。"""
                    tools, approvals = await make_worker_tool_resolver(sessions)(db, run)
                    return [*tools, func1], approvals

                tool_model = SkillModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {
                                        "file_path": "/skills/contract-review/SKILL.md",
                                    },
                                    "id": "read-tool-report-skill",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "func1",
                                    "args": {},
                                    "id": "fetch-report-data",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "render_skill_document",
                                    "args": {
                                        "skill_id": skill_id,
                                        "report_data": {
                                            "title": "工具数据审查报告",
                                            "summary": "工具取数：合同编号 TEST-001，付款期限 15 天。",
                                            "findings": [],
                                        },
                                        "filename": "工具数据报告.docx",
                                    },
                                    "id": "render-tool-report",
                                },
                            ],
                        ),
                        AIMessage(content="已使用工具数据生成报告。"),
                    ],
                )
                finished = await execute_text_run(
                    sessions,
                    tool_run,
                    checkpoint_path=tmp_path / "checkpoints.db",
                    model=tool_model,
                    resolve_tools=tools_with_func1,
                    lease_seconds=10,
                    poll_seconds=0.2,
                )
                assert finished.state == "completed", finished.failure_code
                assert tool_calls == ["func1"]
                tool_messages = (await client.get(path + "/messages", headers=owner)).json()["data"]
                report_id = tool_messages[-1]["artifacts"][0]["artifact_id"]
                generated = await client.get(f"/api/v1/artifacts/{report_id}/download", headers=owner)
                assert generated.status_code == 200
                rendered = Document(io.BytesIO(generated.content))
                assert rendered.paragraphs[1].text == "工具取数：合同编号 TEST-001，付款期限 15 天。"

                async def publish_batch(version: int, entries: list[tuple], headers: dict | None = None):
                    """按文件名提交独立契约，验证批量发布的真实 multipart 请求。"""
                    return await client.post(
                        f"/api/v1/skills/{skill_id}/templates",
                        headers=headers or owner,
                        data={
                            "expected_version": str(version),
                            "fields_schema": json.dumps({name: schema for name, _, schema in entries}),
                        },
                        files=[("files", (name, binary, "application/octet-stream")) for name, binary, _ in entries],
                    )

                batch = [
                    ("A_template.docx", template_bytes("A {{ title }}"), SCHEMA),
                    ("B_template.docx", template_bytes("B {{ title }}"), json.loads(json.dumps(SCHEMA))),
                ]
                batch[1][2]["properties"]["title"]["maxLength"] = 150
                assert (await publish_batch(4, batch, other)).status_code == 404
                before_files = len(list(root.iterdir()))
                invalid_batch = await publish_batch(4, [batch[0], ("bad.docx", b"bad", SCHEMA)])
                assert invalid_batch.status_code == 400
                assert len(list(root.iterdir())) == before_files
                assert (await client.get(f"/api/v1/skills/{skill_id}", headers=owner)).json()["data"]["current_version"] == 4
                duplicate = await publish_batch(4, [("dup.docx", template_bytes(), SCHEMA), ("DUP.DOCX", template_bytes(), SCHEMA)])
                assert duplicate.status_code == 400
                too_many = await publish_batch(4, [(f"template-{number}.docx", template_bytes(), SCHEMA) for number in range(11)])
                assert too_many.status_code == 400
                published_batch = await publish_batch(4, batch)
                assert published_batch.status_code == 201, published_batch.text
                batch_skill = published_batch.json()["data"]
                assert batch_skill["current_version"] == 5 and batch_skill["report_template"] is None
                templates = {entry["filename"]: entry for entry in batch_skill["report_templates"]}
                assert set(templates) == {"新版报告.docx", "A_template.docx", "B_template.docx"}
                assert (await publish_batch(4, batch)).status_code == 409
                legacy = await client.post(
                    endpoint,
                    headers=owner,
                    data={
                        **payload,
                        "expected_version": "5",
                    },
                    files=binary,
                )
                assert legacy.status_code == 409
                revised = await client.put(
                    f"/api/v1/skills/{skill_id}",
                    headers=owner,
                    json={
                        "expected_version": 5,
                        "files": {
                            "SKILL.md": skill_text("contract-review", "按任务选择 A_template.docx 或 B_template.docx；B 用于详细报告。"),
                        },
                    },
                )
                assert revised.status_code == 200, revised.text
                assert len(revised.json()["data"]["report_templates"]) == 3
                multi_agent = await client.post(
                    "/api/v1/agents",
                    headers=owner,
                    json={
                        "name": "多模板助手",
                        "default_model_id": 1,
                        "skill_ids": [skill_id],
                    },
                )
                assert multi_agent.status_code == 201, multi_agent.text
                multi_path = f"/api/v1/agents/{multi_agent.json()['data']['id']}/sessions"
                multi_session = await client.post(multi_path, headers=owner, json={})
                assert multi_session.status_code == 201, multi_session.text
                multi_path += "/" + multi_session.json()["data"]["id"]
                multi_draft = {
                    "input_text": "请生成详细审查报告",
                    "request_key": "multiple-templates",
                }
                multi_created = await client.post(multi_path + "/runs", headers=owner, json=multi_draft)
                assert multi_created.status_code == 201, multi_created.text
                multi_run = multi_created.json()["data"]["id"]
                # 只替换新版本的 B 模板；运行仍固定 v6 的三个资源。
                replaced = await publish_batch(6, [("B_template.docx", template_bytes("最新 B {{ title }}"), batch[1][2])])
                assert replaced.status_code == 201, replaced.text
                replaced_templates = {entry["filename"]: entry for entry in replaced.json()["data"]["report_templates"]}
                assert replaced_templates["A_template.docx"]["artifact_id"] == templates["A_template.docx"]["artifact_id"]
                assert replaced_templates["B_template.docx"]["artifact_id"] != templates["B_template.docx"]["artifact_id"]
                async with sessions() as db:
                    fixed_templates = await skill_reports.templates_for_version(db, skill_id, 6)
                    assert len(fixed_templates) == 3
                    with pytest.raises(ValueError, match="template_name"):
                        await skill_reports.template_for_version(db, skill_id, 6)
                    assert await skill_reports.template_for_version(db, skill_id, 6, "missing.docx") is None
                    selected = await skill_reports.template_for_version(db, skill_id, 6, "b_TEMPLATE.DOCX")
                    assert selected.artifact_id == templates["B_template.docx"]["artifact_id"]
                    files = await load_run_skill_files(db, multi_run, 1)
                    assert all(name in files["/skills/contract-review/SKILL.md"] for name in templates)
                multi_model = SkillModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {
                                        "file_path": "/skills/contract-review/SKILL.md",
                                    },
                                    "id": "read-multiple-templates",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "render_skill_document",
                                    "args": {
                                        "skill_id": skill_id,
                                        "template_name": "B_template.docx",
                                        "report_data": {
                                            "title": "乙" * 110,
                                            "summary": "使用 B 模板独立字段契约。",
                                            "findings": [],
                                        },
                                        "filename": "详细报告.docx",
                                    },
                                    "id": "render-selected-template",
                                },
                            ],
                        ),
                        AIMessage(content="详细报告已生成。"),
                    ],
                )
                multi_result = await execute_text_run(
                    sessions,
                    multi_run,
                    checkpoint_path=tmp_path / "checkpoints.db",
                    model=multi_model,
                    resolve_tools=make_worker_tool_resolver(sessions),
                    lease_seconds=10,
                    poll_seconds=0.2,
                )
                assert multi_result.state == "completed", multi_result.failure_code
                report_message = (await client.get(multi_path + "/messages", headers=owner)).json()["data"][-1]
                assert len(report_message["artifacts"]) == 1
                selected_report = report_message["artifacts"][0]
                assert selected_report["template_source"]["template_artifact_id"] == templates["B_template.docx"]["artifact_id"]
                assert selected_report["template_source"]["template_name"] == "B_template.docx"
                assert selected_report["template_source"]["skill_version"] == 6
                selected_file = await client.get(f"/api/v1/artifacts/{selected_report['artifact_id']}/download", headers=owner)
                assert selected_file.status_code == 200
                assert Document(io.BytesIO(selected_file.content)).paragraphs[0].text == "B " + "乙" * 110
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            await engine.dispose()

    asyncio.run(exercise())


def test_required_reports_cover_all_activated_skills() -> None:
    """未读取的 Skill 不强制产物；多个已执行模板逐个校验固定来源。"""

    async def exercise() -> None:
        """使用隔离数据库验证激活、部分交付、版本错误及完整交付。"""
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all(
                    [
                        RunSkill(run_id="reports", skill_id=1, skill_version=2),
                        RunSkill(run_id="reports", skill_id=2, skill_version=3),
                        SkillTemplate(skill_id=1, skill_version=2, template_key="template-a.docx", artifact_id="template-a", fields_schema=SCHEMA),
                        SkillTemplate(skill_id=2, skill_version=3, template_key="template-b.docx", artifact_id="template-b", fields_schema=SCHEMA),
                    ],
                )
            async with sessions.begin() as db:
                assert await skill_reports.required_report_delivered(db, "reports")
                await skill_reports.activate_skill_report(db, "reports", 1)
                assert not await skill_reports.required_report_delivered(db, "reports")
                first = Artifact(
                    id="report-a",
                    owner_id=1,
                    run_id="reports",
                    filename="报告一.docx",
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    byte_size=1,
                    sha256="a" * 64,
                    storage_key="report-a",
                    source=ArtifactOrigin.GENERATED,
                    state=ArtifactState.PENDING,
                    template_source={
                        "skill_id": 1,
                        "skill_version": 2,
                        "template_artifact_id": "template-a",
                    },
                )
                db.add(first)
                assert await skill_reports.required_report_delivered(db, "reports")
                await skill_reports.activate_skill_report(db, "reports", 2)
                assert not await skill_reports.required_report_delivered(db, "reports")
                second = Artifact(
                    id="report-b",
                    owner_id=1,
                    run_id="reports",
                    filename="报告二.docx",
                    media_type=first.media_type,
                    byte_size=1,
                    sha256="b" * 64,
                    storage_key="report-b",
                    source=ArtifactOrigin.GENERATED,
                    state=ArtifactState.PENDING,
                    template_source={
                        "skill_id": 2,
                        "skill_version": 2,
                        "template_artifact_id": "template-b",
                    },
                )
                db.add(second)
                assert not await skill_reports.required_report_delivered(db, "reports")
                second.template_source = {
                    "skill_id": 2,
                    "skill_version": 3,
                    "template_artifact_id": "template-b",
                }
                assert await skill_reports.required_report_delivered(db, "reports")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("part", ["docProps/core.xml", "word/footnotes.xml", "word/endnotes.xml"])
def test_template_rejects_dynamic_content_outside_validated_parts(part: str) -> None:
    """属性、脚注与尾注的动态语法一律拒绝，防止绕过 AST 与输出预算。"""
    import zipfile

    raw = template_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        parts = {item.filename: archive.read(item) for item in archive.infolist()}
    for payload in ("{{ title }}", "{{ title.upper() }}", "{% for item in findings %}{{ item.note }}{% endfor %}"):
        parts[part] = f"<root><text>{payload}</text></root>".encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        with pytest.raises(ValueError, match="其他部件不支持模板语法"):
            skill_reports.validate_docx_template(output.getvalue(), skill_reports.validate_fields_schema(SCHEMA))
    parts[part] = b"<root><text>{</text><text>{ title }}</text></root>"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    with pytest.raises(ValueError, match="其他部件不支持模板语法"):
        skill_reports.validate_docx_template(output.getvalue(), skill_reports.validate_fields_schema(SCHEMA))


def test_template_allows_static_footnotes() -> None:
    """静态脚注允许保留，禁用范围仅针对未受校验的动态模板语法。"""
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(template_bytes())) as source, zipfile.ZipFile(output, "w") as target:
        for entry in source.infolist():
            target.writestr(entry, source.read(entry))
        target.writestr("word/footnotes.xml", "<root><text>静态脚注说明</text></root>")
    skill_reports.validate_docx_template(output.getvalue(), skill_reports.validate_fields_schema(SCHEMA))
