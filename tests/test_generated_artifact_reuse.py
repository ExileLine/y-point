# -*- coding: utf-8 -*-
# @Time    : 2026-10-27 17:02:02
# @Author  : yangyuexiong
# @File    : test_generated_artifact_reuse.py

"""生成产物只能由后续同会话 Run 按需读取。"""

import asyncio
import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import artifacts as artifact_api
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run import AgentRun
from app.models.agent_session import AgentSession
from app.models.artifact import Artifact
from app.models.artifact_source import ArtifactSource
from app.models.base import Base, shanghai_now
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services import worker_tools
from app.services.artifact_renderers import ArtifactSheet, render_document_artifact, render_spreadsheet_artifact
from app.services.artifact_sources import load_read_source_artifact_ids
from app.services.message_attachments import load_message_artifacts
from app.services.run_manager import claim_run, complete_run, create_run, get_owned_messages
from app.services.worker_tools import make_worker_tool_resolver


def test_generated_artifacts_are_read_on_demand_with_session_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证旧产物按需读取、会话隔离和读取来源持久化。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：验证旧产物按需读取、会话隔离和读取来源持久化。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reuse.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "artifacts"
        root.mkdir(mode=0o700)
        monkeypatch.setattr(worker_tools, "artifact_root", lambda: root)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            first_session = AgentSession(
                id="a" * 36, owner_id=1, agent_id=1, thread_id="a" * 35 + "1", title="续编辑", archived=False, version=1
            )
            other_session = AgentSession(
                id="b" * 36, owner_id=1, agent_id=1, thread_id="b" * 35 + "1", title="其他会话", archived=False, version=1
            )
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="reuse-owner", password="unused", role="user", status=1, is_deleted=0))
                db.add(
                    AgentConfig(
                        id=1,
                        owner_id=1,
                        name="Reusable",
                        system_prompt="续编辑助手",
                        default_model_id=1,
                        temperature=Decimal("1"),
                        max_output_tokens=512,
                        enabled=True,
                        version=1,
                        is_deleted=0,
                        status=1,
                    )
                )
                db.add(
                    ProviderConnection(
                        id=1,
                        name="reuse",
                        name_key="reuse",
                        kind="ollama",
                        base_url="http://localhost:11434/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    )
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="reuse-model",
                        display_name="Reuse model",
                        verified_capabilities=["text", "tools"],
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    )
                )
                db.add_all([first_session, other_session])
                await db.flush()
                snapshot = dict(
                    agent_config_version=1,
                    agent_system_prompt="续编辑助手",
                    agent_temperature=Decimal("1.00"),
                    agent_max_output_tokens=512,
                    model_id=1,
                )
                prior = await create_run(
                    db, 1, agent_id=1, session_id=first_session.id, thread_id=first_session.thread_id, input_text="生成文件", **snapshot
                )
                other = await create_run(
                    db, 1, agent_id=1, session_id=other_session.id, thread_id=other_session.thread_id, input_text="其他会话文件", **snapshot
                )
                for run in (prior, other):
                    claimed = await claim_run(db, run.id)
                    await complete_run(db, run.id, claimed.lease_token, final_text="已生成")

                files = [
                    (prior, "notes.md", "text/markdown", b"# First\nReusable notes"),
                    (prior, "page.html", "text/html", b"<p>Visible</p><script>hidden</script>"),
                    (prior, "report.pdf", "application/pdf", render_document_artifact("report.pdf", "# PDF\nReuse PDF")),
                    (
                        prior,
                        "draft.docx",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        render_document_artifact("draft.docx", "# DOCX\nReuse DOCX"),
                    ),
                    (
                        prior,
                        "data.xlsx",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        render_spreadsheet_artifact([ArtifactSheet(name="Results", rows=[["Name", "Value"], ["Reuse XLSX", 1]])]),
                    ),
                    (other, "private.txt", "text/plain", b"Other session secret"),
                ]
                artifact_ids = {}
                for index, (run, filename, media_type, content) in enumerate(files, start=1):
                    key = f"{index:032x}"
                    artifact = Artifact(
                        id=f"{index:08d}-0000-4000-8000-000000000000",
                        owner_id=1,
                        run_id=run.id,
                        filename=filename,
                        media_type=media_type,
                        byte_size=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                        storage_key=key,
                        source="generated",
                        state="ready",
                        is_deleted=0,
                        status=1,
                    )
                    db.add(artifact)
                    (root / key).write_bytes(content)
                    artifact_ids[filename] = artifact.id
                current = await create_run(
                    db,
                    1,
                    agent_id=1,
                    session_id=first_session.id,
                    thread_id=first_session.thread_id,
                    input_text="修改先前的文件",
                    **snapshot,
                )
                await claim_run(db, current.id)

            async with sessions() as db:
                tools, approvals = await make_worker_tool_resolver(sessions)(db, current)
            assert approvals == {}
            by_name = {item.name: item for item in tools}
            listed = json.loads(await by_name["list_session_artifacts"].ainvoke({}))
            assert {item["filename"] for item in listed} == {"notes.md", "page.html", "report.pdf", "draft.docx", "data.xlsx"}
            expected = {
                "notes.md": "Reusable notes",
                "page.html": "Visible",
                "report.pdf": "Reuse PDF",
                "draft.docx": "Reuse DOCX",
                "data.xlsx": "Reuse XLSX",
            }
            for filename, term in expected.items():
                result = json.loads(await by_name["read_generated_artifact"].ainvoke({"artifact_id": artifact_ids[filename]}))
                assert result["artifact_id"] == artifact_ids[filename]
                assert term in result["content"]
                if filename == "page.html":
                    assert "hidden" not in result["content"]
            with pytest.raises(ValueError, match="产物不可用"):
                await by_name["read_generated_artifact"].ainvoke({"artifact_id": artifact_ids["private.txt"]})
            await by_name["read_generated_artifact"].ainvoke({"artifact_id": artifact_ids["notes.md"]})
            generated = json.loads(await by_name["create_text_artifact"].ainvoke({"filename": "merged.txt", "content": "合并后的内容"}))
            source_ids = [artifact_ids[filename] for filename in expected]
            async with sessions() as db:
                assert await load_read_source_artifact_ids(db, 1, [generated["artifact_id"]]) == {generated["artifact_id"]: source_ids}
                assert await load_read_source_artifact_ids(db, 2, [generated["artifact_id"]]) == {}
                links = (
                    await db.scalars(select(ArtifactSource).where(ArtifactSource.target_artifact_id == generated["artifact_id"]))
                ).all()
                assert [link.position for link in links] == list(range(len(source_ids)))
            async with sessions.begin() as db:
                deleted = await db.get(Artifact, artifact_ids["notes.md"])
                deleted.state = "deleted"
                expired = await db.get(Artifact, artifact_ids["page.html"])
                expired.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            (root / f"{3:032x}").write_bytes(b"changed")
            for filename, error in (("notes.md", "产物不可用"), ("page.html", "产物不可用"), ("report.pdf", "产物不可读取")):
                with pytest.raises(ValueError, match=error):
                    await by_name["read_generated_artifact"].ainvoke({"artifact_id": artifact_ids[filename]})
            assert len(json.loads(await by_name["list_session_artifacts"].ainvoke({}))) == 3
            async with sessions.begin() as db:
                active = await db.get(AgentRun, current.id)
                await complete_run(db, current.id, active.lease_token, final_text="合并文件已生成")
            async with sessions() as db:
                assert await load_read_source_artifact_ids(db, 1, [generated["artifact_id"]]) == {generated["artifact_id"]: source_ids}
                messages = await get_owned_messages(db, current.id, 1)
                rendered = await load_message_artifacts(db, 1, [messages[-1]])
                assert rendered[messages[-1].id][0]["read_source_artifact_ids"] == source_ids
                actor = await db.get(Admin, 1)
                detail = json.loads((await artifact_api.read_artifact(generated["artifact_id"], actor=actor, db=db)).body)["data"]
                listed = json.loads((await artifact_api.list_run_artifacts(current.id, actor=actor, db=db)).body)["data"]
                assert detail["read_source_artifact_ids"] == source_ids
                assert listed[0]["read_source_artifact_ids"] == source_ids
        finally:
            await engine.dispose()

    asyncio.run(exercise())
