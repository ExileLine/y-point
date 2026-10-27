"""验证常见办公附件进入模型上下文及生成产物的上下文来源引用。"""

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import UploadFile
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run_message import AgentRunMessage
from app.models.artifact import Artifact
from app.models.base import Base
from app.services import message_attachments
from app.services.artifact_renderers import (
    ArtifactSheet, render_document_artifact, render_spreadsheet_artifact,
)
from app.services.artifact_store import save_upload
from app.services.message_attachments import (
    load_message_artifacts, load_model_attachment_context, load_run_context_artifact_ids,
)
from app.services.run_manager import claim_run, complete_run, create_run


def test_office_context_and_generated_artifact_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'context.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        root = tmp_path / "private-artifacts"
        root.mkdir(mode=0o700)
        monkeypatch.setattr(message_attachments, "artifact_root", lambda: root)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sources = [
                ("reference.pdf", render_document_artifact("reference.pdf", "# PDF资料\n\nPDF关键字：赤壁")),
                ("reference.docx", render_document_artifact("reference.docx", "# DOCX资料\n\nDOCX关键字：夷陵")),
                ("reference.xlsx", render_spreadsheet_artifact([
                    ArtifactSheet(name="数据表", rows=[["事件", "年份"], ["夷陵之战", 222]]),
                ])),
                ("display.txt", b"display only"),
            ]
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="context-owner", password="unused", role="user", status=1, is_deleted=0))
                await db.flush()
                artifacts = []
                for filename, content in sources:
                    artifact, _ = await save_upload(UploadFile(file=io.BytesIO(content), filename=filename), 1, root)
                    db.add(artifact)
                    artifacts.append(artifact)
                run = await create_run(db, 1, input_text="请总结参考资料")
                message_id = await db.scalar(select(AgentRunMessage.id).where(AgentRunMessage.run_id == run.id))
                db.add_all([
                    AgentMessageAttachment(
                        message_id=message_id, artifact_id=item.id, position=position,
                        filename=item.filename, media_type=item.media_type, byte_size=item.byte_size,
                        included_in_context=position < 3,
                    )
                    for position, item in enumerate(artifacts)
                ])
            async with sessions() as db:
                context = await load_model_attachment_context(db, run.id, 1)
                assert context is not None
                for term in ("赤壁", "夷陵", "数据表", "222"):
                    assert term in context
                assert "display only" not in context
                assert await load_run_context_artifact_ids(db, 1, [run.id]) == {
                    run.id: [item.id for item in artifacts[:3]],
                }
            async with sessions.begin() as db:
                claimed = await claim_run(db, run.id)
                await complete_run(db, run.id, claimed.lease_token, final_text="已生成总结")
                db.add(Artifact(
                    id="11111111-1111-4111-8111-111111111111", owner_id=1, run_id=run.id,
                    filename="summary.txt", media_type="text/plain", byte_size=7, sha256="a" * 64,
                    storage_key="a" * 32, source="generated", state="ready", is_deleted=0, status=1,
                ))
            async with sessions() as db:
                assistant = await db.scalar(select(AgentRunMessage).where(
                    AgentRunMessage.run_id == run.id, AgentRunMessage.role == "assistant",
                ))
                linked = await load_message_artifacts(db, 1, [assistant])
                assert linked[assistant.id][0]["context_artifact_ids"] == [item.id for item in artifacts[:3]]
                assert await load_run_context_artifact_ids(db, 2, [run.id]) == {}
            async with sessions.begin() as db:
                source = await db.get(Artifact, artifacts[0].id)
                source.state = "deleted"
                source.is_deleted = 1
            async with sessions() as db:
                assert (await load_message_artifacts(db, 1, [assistant]))[assistant.id][0][
                    "context_artifact_ids"
                ] == [item.id for item in artifacts[:3]]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_unreadable_or_oversized_document_is_rejected() -> None:
    from app.services.attachment_text import extract_context_text

    with pytest.raises(ValueError):
        extract_context_text(b"%PDF-1.4\n%%EOF", "application/pdf")
    blank = io.BytesIO()
    page = canvas.Canvas(blank)
    page.showPage()
    page.save()
    with pytest.raises(ValueError, match="文本"):
        extract_context_text(blank.getvalue(), "application/pdf")
    large = render_document_artifact("large.docx", "x" * (16 * 1024 + 1))
    with pytest.raises(ValueError, match="上限"):
        extract_context_text(large, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
