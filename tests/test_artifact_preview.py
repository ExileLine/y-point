# -*- coding: utf-8 -*-
# @Time    : 2026-11-04 10:46:22
# @Author  : yangyuexiong
# @File    : test_artifact_preview.py

"""验证真实文档解析、有界预览、文件授权与失效处理。"""

import asyncio
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from docx import Document
from openpyxl import Workbook
from pypdf import PdfWriter
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1 import artifacts as artifact_api
from app.core.enums import ArtifactOrigin, ArtifactPreviewKind, ArtifactState
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import Base, shanghai_now
from app.services import artifact_preview as preview_service
from app.services.artifact_preview import DOCX_MEDIA_TYPE, XLSX_MEDIA_TYPE, build_artifact_preview
from app.services.artifact_renderers import render_document_artifact


def test_text_preview_formats_and_html_isolation() -> None:
    """文本不被误作 Markdown，HTML 去掉外部资源与事件，表格保留原值。"""
    text = "# 这只是普通文本\n<script>alert(1)</script>"
    result = build_artifact_preview("id", "text.md", "text/plain", text.encode())
    assert result.kind == ArtifactPreviewKind.TEXT and result.content == text
    result = build_artifact_preview("id", "report.md", "text/markdown", "# 标题\n- 正文".encode())
    assert result.kind == ArtifactPreviewKind.MARKDOWN and result.content.startswith("# 标题")
    result = build_artifact_preview("id", "data.json", "application/json", b'{"amount":123}')
    assert result.content == '{\n  "amount": 123\n}'
    content = (
        b'<h1 onclick="alert(1)">Title</h1><img src="https://example.test/track">'
        b'<iframe src="https://example.test"></iframe><a href="javascript:alert(1)">Link</a><script>alert(1)</script>'
    )
    result = build_artifact_preview("id", "report.html", "text/html", content)
    assert "<h1>Title</h1>" in result.content
    assert all(value not in result.content for value in ["<script", "onclick", "<img", "<iframe", "href=", "https://"])
    result = build_artifact_preview("id", "data.csv", "text/csv", '名称,说明\n项目,"含逗号,和\n换行"\n公式,=1+1'.encode())
    assert result.kind == ArtifactPreviewKind.SHEETS
    assert result.sheets[0].rows == [["名称", "说明"], ["项目", "含逗号,和\n换行"], ["公式", "=1+1"]]
    assert not result.truncated


def test_office_and_pdf_preview_preserves_content_order() -> None:
    """用真实 DOCX、PDF、XLSX 验证段落表格顺序、多工作表和不计算公式。"""
    document = Document()
    document.add_heading("差旅报告", level=1)
    document.add_paragraph("正文 <script> 不可执行")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "金额"
    table.cell(0, 1).text = "100"
    document.add_paragraph("结束")
    output = BytesIO()
    document.save(output)
    result = build_artifact_preview("id", "report.docx", DOCX_MEDIA_TYPE, output.getvalue())
    assert "<h1>差旅报告</h1>" in result.content and "&lt;script&gt;" in result.content
    assert result.content.index("正文") < result.content.index("<table>") < result.content.index("结束")
    assert result.notice and not result.truncated
    data = render_document_artifact("report.pdf", "# PDF preview\nTravel report body")
    result = build_artifact_preview("id", "report.pdf", "application/pdf", data)
    assert "第 1 页" in result.content and "Travel report body" in result.content
    workbook = Workbook()
    workbook.active.title = "统计"
    workbook.active.append(["月份", "金额"])
    workbook.active.append(["十一月", 100])
    workbook.create_sheet("公式").append(["=1+1", "https://example.test/"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    result = build_artifact_preview("id", "data.xlsx", XLSX_MEDIA_TYPE, output.getvalue())
    assert [sheet.name for sheet in result.sheets] == ["统计", "公式"]
    assert result.sheets[0].rows == [["月份", "金额"], ["十一月", "100"]]
    assert result.sheets[1].rows[0][0] == "=1+1" and not result.truncated


def test_preview_bounds_and_invalid_containers(monkeypatch) -> None:
    """触发字符、行列、单元格、工作表及页数边界，并拒绝无效容器。"""
    monkeypatch.setattr(preview_service, "MAX_PREVIEW_CHARS", 10)
    result = build_artifact_preview("id", "long.txt", "text/plain", "一".encode() * 11)
    assert result.content == "一" * 10 and result.truncated
    monkeypatch.setattr(preview_service, "MAX_PREVIEW_CHARS", 1000)
    monkeypatch.setattr(preview_service, "MAX_PREVIEW_ROWS", 2)
    monkeypatch.setattr(preview_service, "MAX_PREVIEW_COLUMNS", 2)
    result = build_artifact_preview("id", "long.csv", "text/csv", b"a,b,c\n1,2,3\n4,5,6")
    assert result.truncated and result.sheets[0].rows == [["a", "b"], ["1", "2"]]
    monkeypatch.setattr(preview_service, "MAX_PREVIEW_CELLS", 3)
    result = build_artifact_preview("id", "long.csv", "text/csv", b"a,b\n1,2")
    assert result.truncated and result.sheets[0].rows == [["a", "b"], ["1"]]
    monkeypatch.setattr(preview_service, "MAX_PREVIEW_SHEETS", 1)
    workbook = Workbook()
    workbook.active.append(["a"])
    workbook.create_sheet("second").append(["b"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    result = build_artifact_preview("id", "sheets.xlsx", XLSX_MEDIA_TYPE, output.getvalue())
    assert result.truncated and len(result.sheets) == 1
    writer = PdfWriter()
    for _ in range(21):
        writer.add_blank_page(width=100, height=100)
    output = BytesIO()
    writer.write(output)
    result = build_artifact_preview("id", "report.pdf", "application/pdf", output.getvalue())
    assert result.truncated and result.content.count("<h2>") == 20
    writer.encrypt("private-password")
    output = BytesIO()
    writer.write(output)
    with pytest.raises(ValueError, match="加密 PDF"):
        build_artifact_preview("id", "encrypted.pdf", "application/pdf", output.getvalue())
    for filename, media_type, content in [("bad.docx", DOCX_MEDIA_TYPE, b"not-zip"), ("bad.pdf", "application/pdf", b"not-pdf")]:
        with pytest.raises(Exception):
            build_artifact_preview("id", filename, media_type, content)
    with pytest.raises(ValueError):
        build_artifact_preview("id", "image.png", "image/png", b"image")
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for index in range(1001):
            archive.writestr(str(index), "")
    with pytest.raises(ValueError, match="超过预览限制"):
        build_artifact_preview("id", "many.docx", DOCX_MEDIA_TYPE, output.getvalue())


def test_preview_api_authorization_storage_and_errors(tmp_path: Path, monkeypatch) -> None:
    """正式路由校验所有权、过期和删除状态，兼容私有存储且不泄漏路径。"""

    async def exercise() -> None:
        """在隔离库和临时文件中检查真实预览与下载接口契约。"""
        root = tmp_path / "files"
        root.mkdir()
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'preview.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        actor_id = 1
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        async def database():
            """将正式路由绑定到隔离数据库。"""
            async with sessions() as db:
                yield db

        async def actor():
            """只覆盖身份提供器，保留业务文件授权检查。"""
            return Admin(id=actor_id, username="preview-user")

        monkeypatch.setitem(app.dependency_overrides, get_db_session, database)
        monkeypatch.setitem(app.dependency_overrides, get_current_admin, actor)
        monkeypatch.setattr(artifact_api, "artifact_root", lambda: root)
        identifiers = {}
        try:
            async with sessions.begin() as db:
                for label in ["ready", "deleted", "pending", "expired", "missing", "broken", "unsupported"]:
                    identifier = str(uuid4())
                    identifiers[label] = identifier
                    key = uuid4().hex
                    content = b"not-docx" if label == "broken" else "# 真实文档\n预览正文".encode()
                    if label != "missing":
                        (root / key).write_bytes(content)
                    db.add(
                        Artifact(
                            id=identifier,
                            owner_id=1,
                            filename="report.docx" if label == "broken" else "report.md",
                            media_type=DOCX_MEDIA_TYPE if label == "broken" else "image/png" if label == "unsupported" else "text/markdown",
                            byte_size=len(content),
                            sha256="a" * 64,
                            storage_key=key,
                            source=ArtifactOrigin.GENERATED,
                            state=ArtifactState.PENDING if label == "pending" else ArtifactState.READY,
                            is_deleted=1 if label == "deleted" else 0,
                            expires_at=shanghai_now(naive=True) - timedelta(seconds=1) if label == "expired" else None,
                        )
                    )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                url = f"/api/v1/artifacts/{identifiers['ready']}/preview"
                response = await client.get(url)
                assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
                assert response.headers["x-content-type-options"] == "nosniff"
                data = response.json()["data"]
                assert data["kind"] == "markdown" and "预览正文" in data["content"]
                assert "storage_key" not in data and "owner_id" not in data and str(root) not in response.text
                original = await client.get(f"/api/v1/artifacts/{identifiers['ready']}/download")
                assert original.status_code == 200 and original.content.decode().startswith("# 真实文档")
                actor_id = 2
                assert (await client.get(url)).status_code == 404
                actor_id = 1
                assert (await client.get("/api/v1/artifacts/does-not-exist/preview")).status_code == 404
                for label in ["deleted", "pending", "expired", "missing"]:
                    assert (await client.get(f"/api/v1/artifacts/{identifiers[label]}/preview")).status_code == 404
                assert (await client.get(f"/api/v1/artifacts/{identifiers['unsupported']}/preview")).status_code == 415
                response = await client.get(f"/api/v1/artifacts/{identifiers['broken']}/preview")
                assert response.status_code == 422 and "not-docx" not in response.text

                async def object_content(root_path: Path, key: str, *, max_bytes: int) -> bytes:
                    """模拟私有对象读取，确认预览没有绑定本地路径或公开 URL。"""
                    assert root_path == root and max_bytes == 20 * 1024 * 1024
                    return b"# Private object preview"

                monkeypatch.setattr(artifact_api, "read_blob", object_content)
                assert "Private object preview" in (await client.get(url)).json()["data"]["content"]
                actor_id = 2
                assert (await client.get(url)).status_code == 404
                monkeypatch.delitem(app.dependency_overrides, get_current_admin)
                assert (await client.get(url)).status_code == 401
        finally:
            await engine.dispose()

    asyncio.run(exercise())
