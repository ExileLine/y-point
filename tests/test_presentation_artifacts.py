# -*- coding: utf-8 -*-
# @Time    : 2026-11-18 15:29:51
# @Author  : yangyuexiong
# @File    : test_presentation_artifacts.py
"""验证对话 PPTX 的解析、生成、内容预览和实际运行交付。"""

import asyncio
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from langchain_core.messages import AIMessage
from pptx import Presentation
from pptx.util import Inches
from pydantic import ValidationError
from sqlalchemy import select

from app.api.v1 import artifacts as artifact_api
from app.services import message_attachments, worker_tools
from app.services.artifact_intent import requested_artifact_suffixes
from app.services.artifact_preview import build_artifact_preview
from app.services.artifact_store import ArtifactValidationError, validate_file
from app.services.attachment_text import extract_context_text
from app.services.knowledge_formats import PPTX_MEDIA_TYPE
from app.services.presentation_artifacts import ArtifactSlide, ArtifactSlideTable, render_presentation_artifact
from app.services.run_executor import execute_text_run
from app.services.run_manager import get_owned_messages
from app.models.artifact import Artifact
from tests.ag_ui_helpers import submit_agent_run
from tests.test_ag_ui import environment
from tests.test_run_executor import ScriptedModel


def sample_deck() -> bytes:
    """提供含中文、表格和明确备注的真实双页演示文稿。"""
    return render_presentation_artifact(
        [
            ArtifactSlide(
                title="季度经营汇报",
                content="公开正文 <script>alert(1)</script>",
                bullets=["营收增长", "交付及时"],
                notes="仅供演讲者参考：关键结论",
            ),
            ArtifactSlide(title="数据明细", table=ArtifactSlideTable(headers=["项目", "金额"], rows=[["营收", "120万元"], ["成本", "80万元"]])),
        ],
    )


def test_presentation_is_real_editable_office_file(tmp_path: Path) -> None:
    """实际重开 PPTX，验证宽屏页、中文标题、表格、备注及可信媒体类型。"""
    content = sample_deck()
    assert content == sample_deck()
    file = tmp_path / "汇报.pptx"
    file.write_bytes(content)
    assert validate_file(file, file.name) == PPTX_MEDIA_TYPE
    assert validate_file(file, file.name, generated=True) == PPTX_MEDIA_TYPE
    with ZipFile(BytesIO(content)) as archive:
        assert "ppt/presentation.xml" in archive.namelist()
        assert "word/document.xml" not in archive.namelist()
        assert all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in archive.infolist())
    deck = Presentation(BytesIO(content))
    assert len(deck.slides) == 2 and deck.slide_width / deck.slide_height == pytest.approx(16 / 9, abs=0.01)
    assert deck.slides[0].shapes.title.text == "季度经营汇报"
    assert "仅供演讲者参考" in deck.slides[0].notes_slide.notes_text_frame.text
    table = next(shape.table for shape in deck.slides[1].shapes if shape.has_table)
    assert table.cell(1, 1).text == "120万元"
    extracted = extract_context_text(content, PPTX_MEDIA_TYPE)
    assert "第 1 页 · 季度经营汇报" in extracted and "第 2 页 · 数据明细" in extracted
    assert "演讲者备注" in extracted and "120万元" in extracted
    assert extracted.index("公开正文") < extracted.index("仅供演讲者参考") < extracted.index("数据明细")
    preview = build_artifact_preview("id", file.name, PPTX_MEDIA_TYPE, content)
    assert preview.kind == "html" and not preview.truncated
    assert preview.content.count('class="presentation-slide"') == 2
    assert "&lt;script&gt;" in preview.content and "<script>" not in preview.content
    assert "演讲者备注" in preview.content and "120万元" in preview.content
    assert "原始版式" in preview.notice


def test_presentation_excludes_hidden_content_and_external_links() -> None:
    """隐藏页和对象不进模型或预览，包内外链不被访问。"""
    deck = Presentation(BytesIO(sample_deck()))
    deck.slides[1]._element.set("show", "0")
    hidden = deck.slides[0].shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1))
    hidden.text = "隐藏对象机密"
    hidden._element.nvSpPr.cNvPr.set("hidden", "1")
    run = deck.slides[0].shapes.title.text_frame.paragraphs[0].runs[0]
    run.hyperlink.address = "https://example.invalid/never-fetch"
    output = BytesIO()
    deck.save(output)
    text = extract_context_text(output.getvalue(), PPTX_MEDIA_TYPE)
    preview = build_artifact_preview("id", "hidden.pptx", PPTX_MEDIA_TYPE, output.getvalue())
    for value in [text, preview.content]:
        assert "隐藏对象机密" not in value and "120万元" not in value and "never-fetch" not in value
        assert "季度经营汇报" in value
    assert "隐藏幻灯片已排除" in text


def test_presentation_bounds_and_invalid_input(tmp_path: Path, monkeypatch) -> None:
    """过密版面、过多页、结构错误及预览截断均有明确结果，不伪造成功文件。"""
    from app.services import artifact_preview, attachment_text

    with pytest.raises(ValueError, match="1 至"):
        render_presentation_artifact([])
    with pytest.raises(ValueError, match="1 至"):
        render_presentation_artifact([ArtifactSlide(title="一页")] * 51)
    with pytest.raises(ValueError, match="内容过多"):
        render_presentation_artifact([ArtifactSlide(title="长文", bullets=["长" * 240] * 8)])
    with pytest.raises(ValueError, match="不能为空白"):
        render_presentation_artifact([ArtifactSlide(title=" ")])
    with pytest.raises(ValidationError, match="列数"):
        ArtifactSlideTable(headers=["A", "B"], rows=[["one"]])
    file = tmp_path / "fake.pptx"
    file.write_bytes(b"not-office")
    with pytest.raises(ArtifactValidationError):
        validate_file(file, file.name)
    with pytest.raises(ValueError):
        extract_context_text(b"not-office", PPTX_MEDIA_TYPE)
    monkeypatch.setattr(artifact_preview, "MAX_PREVIEW_PAGES", 1)
    preview = build_artifact_preview("id", "deck.pptx", PPTX_MEDIA_TYPE, sample_deck())
    assert preview.truncated and "数据明细" not in preview.content
    monkeypatch.setattr(attachment_text, "context_max_bytes", lambda: 32)
    with pytest.raises(ValueError, match="超过"):
        extract_context_text(sample_deck(), PPTX_MEDIA_TYPE)


@pytest.mark.parametrize("initial_refusal", [False, True])
def test_ag_ui_upload_pptx_reaches_model_and_generates_previewable_pptx(tmp_path: Path, monkeypatch, initial_refusal: bool) -> None:
    """通过真实上传、AG-UI 提交、DeepAgents 和文件工具贯通上下文到可下载产物。"""

    async def exercise() -> None:
        """仅替换上游模型，保留真实鉴权、附件预检、数据库事务和工具执行。"""
        root = tmp_path / "files"
        root.mkdir(mode=0o700)
        for module in [artifact_api, message_attachments, worker_tools]:
            monkeypatch.setattr(module, "artifact_root", lambda: root)
        async with environment(tmp_path) as (sessions, client, headers):
            content = sample_deck()
            upload = await client.post(
                "/api/v1/artifacts/uploads",
                headers=headers,
                files={
                    "files": ("参考资料.pptx", content, PPTX_MEDIA_TYPE),
                },
            )
            assert upload.status_code == 201, upload.text
            uploaded = upload.json()["data"][0]
            assert uploaded["media_type"] == PPTX_MEDIA_TYPE
            props = {
                "artifact_ids": [uploaded["id"]],
                "context_artifact_ids": [uploaded["id"]],
            }
            checked = await client.post("/api/v1/agents/1/sessions/session/attachments/check", headers=headers, json=props)
            assert checked.status_code == 200, checked.text
            accepted = await submit_agent_run(
                client,
                "/api/v1/agents/1/sessions/session/runs",
                headers=headers,
                json={
                    "input_text": "根据附件生成 PPTX 演示文稿",
                    "request_key": "pptx-run",
                    **props,
                },
            )
            assert accepted.status_code == 201, accepted.text
            run_id = accepted.json()["data"]["id"]
            observed = []

            class ReadingModel(ScriptedModel):
                """记录模型真正接收的系统提示和附件上下文，不使用真实供应商。"""

                def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                    """在实际模型调用位置记录输入，然后按脚本返回工具调用。"""
                    observed.extend(messages)
                    return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

            model = ReadingModel(
                responses=[
                    *([AIMessage(content="暂不支持直接生成 PPTX，只能用 DOCX 替代。")] if initial_refusal else []),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "create_presentation_artifact",
                                "args": {
                                    "filename": "季度汇报.pptx",
                                    "slides": [
                                        {
                                            "title": "季度汇报",
                                            "bullets": ["营收120万元"],
                                            "notes": "来自附件数据明细",
                                        }
                                    ],
                                },
                                "id": "pptx-call",
                            }
                        ],
                    ),
                    AIMessage(content="已生成真实的 PowerPoint 演示文稿。"),
                ],
            )
            completed = await execute_text_run(
                sessions,
                run_id,
                model,
                tmp_path / "checkpoint.db",
                resolve_tools=worker_tools.make_worker_tool_resolver(sessions),
            )
            assert completed.state == "completed" and completed.tool_call_count == 1
            model_input = "\n".join(str(message.content) for message in observed)
            assert "120万元" in model_input and "演讲者备注" in model_input
            if initial_refusal:
                assert "上一轮尚未交付满足本轮要求的文件" in model_input
            assert "create_presentation_artifact" in model_input and "不能用 DOCX" in model_input
            async with sessions() as db:
                artifact = await db.scalar(select(Artifact).where(Artifact.run_id == run_id))
                assert artifact.state == "ready" and artifact.filename == "季度汇报.pptx"
                assert requested_artifact_suffixes("生成 PPTX 文件") == frozenset({".pptx"})
                messages = await get_owned_messages(db, run_id, 1)
                assert messages[-1].role == "assistant"
                assert "不支持" not in messages[-1].content and "DOCX" not in messages[-1].content
            response = await client.get(f"/api/v1/artifacts/{artifact.id}/preview", headers=headers)
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert "季度汇报" in response.json()["data"]["content"]
            download = await client.get(f"/api/v1/artifacts/{artifact.id}/download", headers=headers)
            assert download.status_code == 200 and download.headers["content-type"] == PPTX_MEDIA_TYPE
            assert Presentation(BytesIO(download.content)).slides[0].shapes.title.text == "季度汇报"
            # 后续轮次按同一会话读取产物，继续使用公开文本提取与既有来源审计。
            next_run = await submit_agent_run(
                client,
                "/api/v1/agents/1/sessions/session/runs",
                headers=headers,
                json={
                    "input_text": "读取上次生成的文件",
                    "request_key": "pptx-read",
                },
            )
            from app.models.agent_run import AgentRun
            from app.services.run_manager import claim_run

            async with sessions.begin() as db:
                run = await claim_run(db, next_run.json()["data"]["id"])
            async with sessions() as db:
                run = await db.get(AgentRun, run.id)
                tools, _ = await worker_tools.make_worker_tool_resolver(sessions)(db, run)
            read_tool = next(tool for tool in tools if tool.name == "read_generated_artifact")
            result = await read_tool.ainvoke(
                {
                    "artifact_id": artifact.id,
                }
            )
            assert "120万元" in result

    asyncio.run(exercise())


def test_docx_cannot_satisfy_pptx_delivery(tmp_path: Path, monkeypatch) -> None:
    """模型只交付 DOCX 时不得宣告 PPTX 成功，也不发布替代文件。"""

    async def exercise() -> None:
        """沿真实 AG-UI 和执行器流程验证格式校验与失败暂存状态。"""
        root = tmp_path / "files"
        root.mkdir(mode=0o700)
        monkeypatch.setattr(worker_tools, "artifact_root", lambda: root)
        async with environment(tmp_path) as (sessions, client, headers):
            accepted = await submit_agent_run(
                client,
                "/api/v1/agents/1/sessions/session/runs",
                headers=headers,
                json={
                    "input_text": "生成 .pptx 演示文稿",
                    "request_key": "no-docx-substitute",
                },
            )
            model = ScriptedModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "create_document_artifact",
                                "args": {
                                    "filename": "替代.docx",
                                    "content": "# 幻灯片大纲\n用 DOCX 代替",
                                },
                                "id": "docx-substitute",
                            }
                        ],
                    ),
                    AIMessage(content="请把 Word 复制到 PowerPoint。"),
                    AIMessage(content="仅提供 Word 文件。"),
                ],
            )
            run_id = accepted.json()["data"]["id"]
            result = await execute_text_run(sessions, run_id, model, tmp_path / "checkpoint.db", resolve_tools=worker_tools.make_worker_tool_resolver(sessions))
            assert result.state == "failed" and result.failure_code == "artifact_required"
            async with sessions() as db:
                artifacts = (await db.scalars(select(Artifact).where(Artifact.run_id == run_id))).all()
                assert artifacts and all(item.state == "pending" for item in artifacts)
                assert all(message.role != "assistant" for message in await get_owned_messages(db, run_id, 1))

    asyncio.run(exercise())
