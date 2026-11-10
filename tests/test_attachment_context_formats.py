# -*- coding: utf-8 -*-
# @Time    : 2026-10-27 16:47:17
# @Author  : yangyuexiong
# @File    : test_attachment_context_formats.py

"""验证常见办公附件进入模型上下文及生成产物的上下文来源引用。"""

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import UploadFile
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.models.admin import Admin
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run_message import AgentRunMessage
from app.models.artifact import Artifact
from app.models.base import Base
from app.services import message_attachments
from app.services.artifact_renderers import ArtifactSheet, render_document_artifact, render_spreadsheet_artifact
from app.services.artifact_store import save_upload
from app.services.attachment_text import context_max_bytes, extract_context_text
from app.services.message_attachments import load_message_artifacts, load_model_attachment_context, load_run_context_artifact_ids
from app.services.run_manager import claim_run, complete_run, create_run


def test_office_context_and_generated_artifact_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """验证办公附件提取文本及生成产物的上下文来源引用。"""

    async def exercise() -> None:
        """在隔离环境执行异步场景：验证办公附件提取文本及生成产物的上下文来源引用。"""
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
                ("reference.docx", render_document_artifact("reference.docx", "# DOCX资料\n\nDOCX关键字：夷陵" + "正文内容" * 2000)),
                ("reference.xlsx", render_spreadsheet_artifact([ArtifactSheet(name="数据表", rows=[["事件", "年份"], ["夷陵之战", 222]])])),
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
                db.add_all(
                    [
                        AgentMessageAttachment(
                            message_id=message_id,
                            artifact_id=item.id,
                            position=position,
                            filename=item.filename,
                            media_type=item.media_type,
                            byte_size=item.byte_size,
                            included_in_context=position < 3,
                        )
                        for position, item in enumerate(artifacts)
                    ],
                )
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
                db.add(
                    Artifact(
                        id="11111111-1111-4111-8111-111111111111",
                        owner_id=1,
                        run_id=run.id,
                        filename="summary.txt",
                        media_type="text/plain",
                        byte_size=7,
                        sha256="a" * 64,
                        storage_key="a" * 32,
                        source="generated",
                        state="ready",
                        is_deleted=0,
                        status=1,
                    ),
                )
            async with sessions() as db:
                assistant = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == "assistant"))
                linked = await load_message_artifacts(db, 1, [assistant])
                assert linked[assistant.id][0]["context_artifact_ids"] == [item.id for item in artifacts[:3]]
                assert await load_run_context_artifact_ids(db, 2, [run.id]) == {}
            async with sessions.begin() as db:
                source = await db.get(Artifact, artifacts[0].id)
                source.state = "deleted"
                source.is_deleted = 1
            async with sessions() as db:
                assert (await load_message_artifacts(db, 1, [assistant]))[assistant.id][0]["context_artifact_ids"] == [item.id for item in artifacts[:3]]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_unreadable_or_oversized_document_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证不可读或超出上下文大小限制的文档被拒绝。"""
    with pytest.raises(ValueError):
        extract_context_text(b"%PDF-1.4\n%%EOF", "application/pdf")
    blank = io.BytesIO()
    page = canvas.Canvas(blank)
    page.showPage()
    page.save()
    with pytest.raises(ValueError, match="文本"):
        extract_context_text(blank.getvalue(), "application/pdf")
    monkeypatch.setattr(get_config(), "ATTACHMENT_CONTEXT_MAX_BYTES", 1024)
    large = render_document_artifact("large.docx", "x" * 1025)
    with pytest.raises(ValueError, match="上限"):
        extract_context_text(large, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")


def test_attachment_text_accepts_twenty_mib_and_rejects_overflow() -> None:
    """按真实 UTF-8 字节验证 20 MiB 边界，防止只修改提示而没有放开预算。"""
    limit = 20 * 1024 * 1024
    assert context_max_bytes() == limit
    assert extract_context_text(b"x" * limit, "text/plain") == "x" * limit
    with pytest.raises(ValueError, match="20 MiB"):
        extract_context_text(b"x" * (limit + 1), "text/plain")
    with pytest.raises(ValueError, match="20 MiB"):
        extract_context_text(("中" * (limit // 3 + 1)).encode(), "text/plain")


def test_attachment_context_budget_includes_metadata_and_all_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """单文件可读仍须累计 JSON 元信息和其他文件，超限不能静默截断。"""

    async def exercise() -> None:
        """使用真实私有文件验证 worker 与预检查共用的上下文组装逻辑。"""
        monkeypatch.setattr(message_attachments, "artifact_root", lambda: tmp_path)
        payload = b"x" * (10 * 1024 * 1024)
        first, _ = await save_upload(UploadFile(file=io.BytesIO(payload), filename="first.txt"), 1, tmp_path)
        second, _ = await save_upload(UploadFile(file=io.BytesIO(payload), filename="second.txt"), 1, tmp_path)
        context = await message_attachments.build_attachment_context([first])
        assert context is not None and len(context.encode()) > len(payload)
        with pytest.raises(message_attachments.AttachmentContextUnavailable, match="20 MiB"):
            await message_attachments.build_attachment_context([first, second])

    asyncio.run(exercise())
