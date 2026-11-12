# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 22:21:23
# @Author  : yangyuexiong
# @File    : test_knowledge_pdf.py

"""验证 PDF 原始页面、OCR 质量、缓存、模型加工及访问撤销。"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, KnowledgeJobState, KnowledgePdfExtraction
from app.models.knowledge import KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeJob
from app.services import knowledge_enrichment, knowledge_pdf
from app.services.knowledge_parser import KnowledgeProcessingError, chunk_knowledge, parse_knowledge, parse_knowledge_isolated, render_pdf_page_isolated
from scripts.generate_knowledge_pdf_fixtures import make_pdf_fixture
from tests.test_knowledge import _create_kb, _drain, _search, _upload
from tests.test_knowledge_processing import TableExplanationModel, _configure_model
from tests.test_m51_governance import _database, _http_platform


@pytest.mark.parametrize("kind", ["native", "table", "columns"])
def test_pdf_native_geometry_layout_and_exact_offsets(kind: str) -> None:
    """真实 PDF 保留正文、整行表格、双栏阅读顺序及可复核偏移和坐标。"""
    parsed = parse_knowledge(make_pdf_fixture(kind), "application/pdf")
    chunks = chunk_knowledge(parsed, 200, 0)
    assert parsed.metadata["ocr_pages"] == 0
    for chunk in chunks:
        source = chunk["source_locator"]
        assert chunk["content"] == parsed.text[chunk["start_offset"] : chunk["end_offset"]]
        assert source["page"] == 1 and source["extraction_method"] == KnowledgePdfExtraction.NATIVE
        x0, top, x1, bottom = source["bbox"]
        assert 0 <= x0 < x1 <= source["page_width"] and 0 <= top < bottom <= source["page_height"]
    if kind == "table":
        assert all(chunk["source_locator"]["layout"] == "table" for chunk in chunks)
        assert "列1=Alpha；列2=3800；列3=50" in parsed.text
    if kind == "columns":
        assert parsed.text.index("Left item 4") < parsed.text.index("Right item 1")


@pytest.mark.parametrize(
    "kind,mode,failure",
    [("scan", "text", KnowledgeFailure.OCR_REQUIRED), ("blank", "auto", KnowledgeFailure.OCR_REQUIRED), ("encrypted", "auto", KnowledgeFailure.PARSE_FAILED)],
)
def test_pdf_isolated_failure_codes(kind: str, mode: str, failure: KnowledgeFailure) -> None:
    """正式子进程保留需要 OCR、空白和加密文件的脱敏错误类别。"""

    async def exercise() -> None:
        """避免仅测试进程内异常而漏掉模块重复加载造成的类别丢失。"""
        with pytest.raises(KnowledgeProcessingError) as error:
            await parse_knowledge_isolated(
                make_pdf_fixture(kind),
                "application/pdf",
                {
                    "pdf_mode": mode,
                },
            )
        assert error.value.code == failure

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["disabled", "missing", "missing-language", "low-confidence", "timeout", "oversized-output", "bad-geometry"])
def test_pdf_ocr_failures_are_explicit_and_bounded(monkeypatch, mode: str) -> None:
    """隔离替身仅替换 OCR 程序，仍运行真实 PDF 分类、渲染和 TSV 校验。"""
    runtime = knowledge_pdf.pdf_runtime_configuration()
    runtime["ocr_enabled"] = mode != "disabled"
    monkeypatch.setattr(knowledge_pdf.shutil, "which", lambda command: None if mode == "missing" else "/tmp/tesseract-fixture")

    def run(arguments, **options):
        """在真实临时目录生成可控识别输出，覆盖质量与资源边界。"""
        if mode == "timeout":
            raise subprocess.TimeoutExpired(arguments, 35)
        if arguments[1] == "--list-langs":
            return subprocess.CompletedProcess(arguments, 0, stdout=b"eng\n" if mode == "missing-language" else b"eng\nchi_sim\n")
        target = Path(arguments[2]).with_suffix(".tsv")
        if mode == "oversized-output":
            with target.open("wb") as stream:
                stream.truncate(knowledge_pdf.MAX_OCR_BYTES + 1)
        else:
            confidence = 25 if mode == "low-confidence" else 95
            left = 999999 if mode == "bad-geometry" else 120
            target.write_text(
                "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                f"5\t1\t1\t1\t1\t1\t{left}\t200\t100\t30\t{confidence}\t5000\n",
            )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(knowledge_pdf.subprocess, "run", run)
    with pytest.raises(KnowledgeProcessingError) as error:
        parse_knowledge(
            make_pdf_fixture("scan"),
            "application/pdf",
            {
                "_pdf_runtime": runtime,
            },
        )
    expected = {
        "disabled": KnowledgeFailure.OCR_UNAVAILABLE,
        "missing": KnowledgeFailure.OCR_UNAVAILABLE,
        "missing-language": KnowledgeFailure.OCR_UNAVAILABLE,
        "low-confidence": KnowledgeFailure.OCR_LOW_CONFIDENCE,
        "timeout": KnowledgeFailure.TIMEOUT,
        "oversized-output": KnowledgeFailure.SIZE_LIMIT,
        "bad-geometry": KnowledgeFailure.PARSE_FAILED,
    }
    assert error.value.code == expected[mode]


def test_pdf_mixed_pages_quality_and_ocr_page_limit(monkeypatch) -> None:
    """按页混用文字层与 OCR，空白页显式记录且不生成伪证据。"""

    def ocr(page, runtime):
        """使用固定坐标识别行，验证页面分类与摘要而不依赖系统 OCR。"""
        return [("Hotel allowance 5000 CNY", [48.0, 80.0, 400.0, 110.0])], 95.0

    monkeypatch.setattr(knowledge_pdf, "_ocr_page", ocr)
    parsed = parse_knowledge(make_pdf_fixture("mixed"), "application/pdf")
    assert [page["kind"] for page in parsed.metadata["pages"]] == ["text", "scan", "blank"]
    assert parsed.metadata["ocr_pages"] == parsed.metadata["blank_pages"] == 1
    chunks = chunk_knowledge(parsed, 800, 100)
    assert {chunk["page"] for chunk in chunks} == {1, 2}
    assert chunks[-1]["source_locator"]["review_required"] is True
    runtime = knowledge_pdf.pdf_runtime_configuration()
    runtime["max_ocr_pages"] = 1
    with pytest.raises(KnowledgeProcessingError) as error:
        parse_knowledge(
            make_pdf_fixture("mixed"),
            "application/pdf",
            {
                "pdf_mode": "ocr",
                "_pdf_runtime": runtime,
            },
        )
    assert error.value.code == KnowledgeFailure.SIZE_LIMIT


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="需本地 Tesseract 与 chi_sim/eng 语言包")
@pytest.mark.parametrize("kind", ["scan", "chinese-scan"])
def test_pdf_real_local_ocr_and_render(kind: str) -> None:
    """通过正式子进程验证真实 OCR 和原页 PNG，无需远程供应商。"""

    async def exercise() -> None:
        """识别稳定扫描样例并核对真实输出和页面渲染。"""
        available = subprocess.run([shutil.which("tesseract"), "--list-langs"], capture_output=True, text=True, check=True).stdout.splitlines()
        if not {"chi_sim", "eng"}.issubset(available):
            pytest.skip("缺少 chi_sim/eng OCR 语言包")
        content = make_pdf_fixture(kind)
        parsed = await parse_knowledge_isolated(content, "application/pdf")
        if kind == "scan":
            assert "5000" in parsed.text and "Hotel" in parsed.text
        else:
            normalized = parsed.text.replace(" ", "")
            assert "报销" in normalized and "五千元" in normalized
        assert parsed.metadata["ocr_pages"] == 1
        assert parsed.metadata["pages"][0]["confidence"] >= 60
        image = await render_pdf_page_isolated(content, 1)
        assert image.startswith(b"\x89PNG\r\n\x1a\n")

    asyncio.run(exercise())


def test_pdf_http_preview_cache_options_and_revocation(tmp_path, monkeypatch) -> None:
    """正式 HTTP 和 worker 保留证据摘要，修改提取选项生成新版本，撤权立即阻断原页。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """覆盖原页二次鉴权、缓存重建、文件去重与处理模式适用范围。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            kb = await _create_kb(client, owner)
            content = make_pdf_fixture("table")
            uploaded = await _upload(client, owner, kb, "报价.pdf", content)
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "Alpha"))["hits"][0]
            preview_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{uploaded['document']['id']}/processing-preview"
            preview = (await client.get(preview_url, headers=owner)).json()["data"]
            assert preview["processing_metadata"]["pages"][0]["kind"] == "text"
            image_url = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}/page-image"
            image = await client.get(image_url, headers=owner)
            assert image.status_code == 200 and image.content.startswith(b"\x89PNG\r\n\x1a\n")
            assert image.headers["cache-control"] == "no-store"
            assert (await client.get(image_url, headers=other)).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/knowledge-bases/{kb['id']}/shares",
                    headers=owner,
                    json={
                        "user_id": 2,
                    },
                )
            ).status_code == 200
            assert (await client.get(image_url, headers=other)).status_code == 200
            assert (await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/shares/2", headers=owner)).status_code == 200
            assert (await client.get(image_url, headers=other)).status_code == 404

            async def forbidden(*arguments, **options):
                """相同指纹重建应使用解析缓存，不再次调用解析器。"""
                raise AssertionError("缓存重建不应重新解析")

            from app.services import knowledge_jobs

            original = knowledge_jobs.parse_knowledge_isolated
            monkeypatch.setattr(knowledge_jobs, "parse_knowledge_isolated", forbidden)
            rebuild = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            kb["revision"] = rebuild.json()["data"]["revision"]
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, rebuild.json()["data"]["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED
            monkeypatch.setattr(knowledge_jobs, "parse_knowledge_isolated", original)
            updated = await client.put(
                f"/api/v1/knowledge-bases/{kb['id']}/documents/{uploaded['document']['id']}",
                headers=owner,
                data={
                    "expected_revision": str(kb["revision"]),
                    "processing_options": json.dumps(
                        {
                            "pdf_mode": "text",
                        }
                    ),
                },
                files={
                    "file": ("报价.pdf", content),
                },
            )
            assert updated.status_code == 202 and updated.json()["data"]["document"]["current_version"] == 2
            await _drain(sessions)
            preview = (await client.get(preview_url, headers=owner)).json()["data"]
            assert preview["indexed_version"] == 2 and preview["processing_options"]["pdf_mode"] == "text"
            async with sessions() as db:
                version = await db.scalar(select(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.version == 2))
                chunks = (await db.scalars(select(KnowledgeChunk).where(KnowledgeChunk.document_version_id == version.id))).all()
                assert all(chunk.content == version.extracted_text[chunk.start_offset : chunk.end_offset] for chunk in chunks)
            invalid = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                headers=owner,
                data={
                    "expected_revision": str(updated.json()["data"]["revision"]),
                    "processing_options": json.dumps(
                        {
                            "pdf_mode": "ocr",
                        }
                    ),
                },
                files={
                    "file": ("不适用.txt", b"not a pdf"),
                },
            )
            assert invalid.status_code == 422

    asyncio.run(exercise())


@pytest.mark.parametrize("kind,mode,calls", [("native", "auto", 0), ("native", "enhanced", 1), ("table", "auto", 1), ("table", "original", 0)])
def test_pdf_enrichment_routing_and_original_evidence(tmp_path, monkeypatch, kind: str, mode: str, calls: int) -> None:
    """纯文字不自动加工，表格自动加工，增强和原文策略保持原始证据不变。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    model = TableExplanationModel()
    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **options: model)

    async def exercise() -> None:
        """通过正式构建读取加工计量与证据内容，避免只测试路由条件。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            await _configure_model(sessions)
            kb = await _create_kb(client, headers[0], processing_mode=mode, processing_model_id=1)
            uploaded = await _upload(client, headers[0], kb, "证据.pdf", make_pdf_fixture(kind))
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED and model.calls == calls
                version = await db.scalar(select(KnowledgeDocumentVersion))
                chunks = (await db.scalars(select(KnowledgeChunk))).all()
                assert chunks and all(chunk.content == version.extracted_text[chunk.start_offset : chunk.end_offset] for chunk in chunks)
                assert all(bool(chunk.knowledge_notes) == bool(calls) for chunk in chunks)

    asyncio.run(exercise())


def test_pdf_metadata_migration_preserves_old_text(tmp_path) -> None:
    """隔离迁移新增空摘要，回退重升不影响旧版本原文。"""
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "pdf-migration.db"
    environment = {
        **os.environ,
        "DB_BACKEND": "sqlite",
        "SQLITE_DATABASE": str(database),
    }

    def migrate(action: str, revision: str) -> None:
        """操作隔离数据库并保留必要迁移错误。"""
        result = subprocess.run([sys.executable, "-m", "alembic", action, revision], cwd=root, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "051")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO yp_knowledge_document_version
            (id,document_id,version,artifact_id,filename,media_type,sha256,byte_size,extracted_text,blocks,
            create_time,update_time,create_timestamp,update_timestamp)
            VALUES (1,1,1,'fixture','old.pdf','application/pdf','hash',1,'old evidence','[]',
            '2026-11-12 00:00:00','2026-11-12 00:00:00',0,0)""",
        )
        connection.execute(
            """INSERT INTO yp_knowledge_index
            (id,knowledge_base_id,revision,state,configuration,document_versions,chunk_count,
            create_time,update_time,create_timestamp,update_timestamp)
            VALUES (1,1,1,'ready','{}','[1]',0,'2026-11-12 00:00:00','2026-11-12 00:00:00',0,0)""",
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT extracted_text,processing_metadata FROM yp_knowledge_document_version").fetchone() == ("old evidence", "{}")
        assert connection.execute("SELECT configuration,processing_metadata FROM yp_knowledge_index").fetchone() == ("{}", "{}")
    migrate("downgrade", "051")
    migrate("upgrade", "head")
    result = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=root, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_pdf_failure_keeps_previous_index_and_rechecks_page_permission(tmp_path, monkeypatch) -> None:
    """OCR 不可用保留旧活动代，原页渲染等待期间撤销权限也不能返回图片。"""
    from app.api.v1 import knowledge as knowledge_api

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_ENABLED", False)

    async def exercise() -> None:
        """用正式路由交错共享撤销与图片生成，核对返回边界及旧引用可用。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            kb = await _create_kb(client, owner)
            await _upload(client, owner, kb, "规则.pdf", make_pdf_fixture("native"))
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "Hotel"))["hits"][0]
            failed = await _upload(client, owner, kb, "扫描.pdf", make_pdf_fixture("scan"))
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, failed["job_id"])
                assert job.state == KnowledgeJobState.FAILED and job.failure_code == KnowledgeFailure.OCR_UNAVAILABLE
            assert (await _search(client, owner, kb["id"], "Hotel"))["hits"][0]["index_id"] == hit["index_id"]
            granted = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/shares",
                headers=owner,
                json={
                    "user_id": 2,
                },
            )
            assert granted.status_code == 200
            entered, release = asyncio.Event(), asyncio.Event()

            async def render(*arguments):
                """暂停在外部渲染等待处，为撤权提供确定性时机。"""
                entered.set()
                await release.wait()
                return b"\x89PNG\r\n\x1a\n"

            monkeypatch.setattr(knowledge_api, "render_pdf_page_isolated", render)
            pending = asyncio.create_task(client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}/page-image", headers=other))
            await asyncio.wait_for(entered.wait(), timeout=5)
            assert (await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/shares/2", headers=owner)).status_code == 200
            release.set()
            assert (await pending).status_code == 404

    asyncio.run(exercise())


@pytest.mark.parametrize("variant", ["crop", "rotate", "offset"])
def test_pdf_page_geometry_matches_original_render(variant: str) -> None:
    """裁剪、旋转及非零页面原点下，坐标尺寸与完整原页图像保持一致。"""
    import io

    from PIL import Image, ImageChops
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject

    writer = PdfWriter()
    page = PdfReader(io.BytesIO(make_pdf_fixture("native"))).pages[0]
    if variant == "crop":
        page.cropbox = RectangleObject([20, 200, 580, 780])
    elif variant == "rotate":
        page.rotate(90)
    else:
        page.mediabox = RectangleObject([-20, -30, 580, 770])
    writer.add_page(page)
    output = io.BytesIO()
    writer.write(output)
    parsed = parse_knowledge(output.getvalue(), "application/pdf")
    with Image.open(io.BytesIO(knowledge_pdf.render_pdf_page(output.getvalue(), 1))) as image:
        for block in parsed.blocks:
            source = block["source_locator"]
            assert abs(image.width / image.height - source["page_width"] / source["page_height"]) < 0.002
            x0, top, x1, bottom = source["bbox"]
            assert 0 <= x0 < x1 <= source["page_width"] and 0 <= top < bottom <= source["page_height"]
            box = (
                x0 * image.width / source["page_width"],
                top * image.height / source["page_height"],
                x1 * image.width / source["page_width"],
                bottom * image.height / source["page_height"],
            )
            region = image.crop(box).convert("RGB")
            assert ImageChops.difference(region, Image.new("RGB", region.size, "white")).getbbox() is not None


def test_pdf_small_image_page_is_not_silently_skipped(monkeypatch) -> None:
    """含小图但无文字的页面必须进入 OCR，不能当作空白页丢失内容。"""
    import io

    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas

    output = io.BytesIO()
    canvas = Canvas(output, pagesize=(600, 800))
    image = Image.new("RGB", (100, 100), "black")
    canvas.drawImage(ImageReader(image), 50, 650, width=100, height=100)
    canvas.showPage()
    canvas.save()
    monkeypatch.setattr(knowledge_pdf, "_ocr_page", lambda page, runtime: ([("image evidence", [50, 50, 150, 150])], 95))
    parsed = parse_knowledge(output.getvalue(), "application/pdf")
    assert parsed.metadata["ocr_pages"] == 1 and parsed.text == "image evidence"


@pytest.mark.skipif(os.name != "posix", reason="进程组终止仅适用于 POSIX")
@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
def test_pdf_parser_interruption_removes_private_temp_and_children(tmp_path, monkeypatch, interruption: str) -> None:
    """取消和超时终止解析进程与模拟 OCR 子进程，并删除中间图片目录。"""
    from app.services import knowledge_parser

    marker = tmp_path / "process.json"
    script = tmp_path / "parser.py"
    script.write_text(
        "import json, os, pathlib, subprocess, sys, time\n"
        "root = pathlib.Path(os.environ['TMPDIR'])\n"
        "(root / 'private-page.png').write_bytes(b'private fixture')\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(dict(root=str(root), child=child.pid)))\n"
        "time.sleep(60)\n",
    )
    real_create, real_wait = asyncio.create_subprocess_exec, asyncio.wait_for
    processes = []

    async def create(*arguments, **options):
        """替换解析入口，仍使用正式隔离会话、管道和临时目录管理。"""
        process = await real_create(sys.executable, str(script), **options)
        processes.append(process)
        return process

    async def wait(awaitable, timeout):
        """只缩短正式解析超时，不改变中断清理代码。"""
        return await real_wait(awaitable, timeout=1 if interruption == "timeout" else timeout)

    monkeypatch.setattr(knowledge_parser.asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(knowledge_parser.asyncio, "wait_for", wait)

    async def exercise() -> None:
        """等子进程实际建立后触发中断，核对私有数据和存活状态。"""
        pending = asyncio.create_task(parse_knowledge_isolated(b"fixture", "application/pdf"))
        try:
            for _ in range(100):
                if marker.exists():
                    break
                await asyncio.sleep(0.02)
            assert marker.exists()
            if interruption == "cancel":
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            else:
                with pytest.raises(KnowledgeProcessingError) as error:
                    await pending
                assert error.value.code == KnowledgeFailure.TIMEOUT
            state = json.loads(marker.read_text())
            assert not Path(state["root"]).exists()
            assert processes[0].returncode is not None
            child = subprocess.run(["ps", "-p", str(state["child"]), "-o", "stat="], capture_output=True, text=True)
            assert not child.stdout.strip() or child.stdout.strip().startswith("Z")
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending

    asyncio.run(exercise())


def test_pdf_failed_reparse_does_not_change_published_metadata(tmp_path, monkeypatch) -> None:
    """重建更新了解析缓存后失败，活动代的逐页摘要和证据仍属于原已发布结果。"""
    from dataclasses import replace

    from app.services import knowledge_jobs

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    original_parse = knowledge_jobs.parse_knowledge_isolated

    async def reparse(*arguments, **options):
        """模拟解析器配置变更产生新摘要，不改变原始文件版本。"""
        parsed = await original_parse(*arguments, **options)
        return replace(
            parsed,
            metadata={
                **parsed.metadata,
                "parser": "candidate-parse",
            },
        )

    def fail(*arguments):
        """让新解析缓存已经保存后分块失败，复现旧预览混入新摘要的时机。"""
        raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)

    async def exercise() -> None:
        """经正式 HTTP、worker 和活动预览校验发布事务中的摘要边界。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, _ = headers
            kb = await _create_kb(client, owner)
            uploaded = await _upload(client, owner, kb, "规则.pdf", make_pdf_fixture("native"))
            await _drain(sessions)
            preview_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{uploaded['document']['id']}/processing-preview"
            previous = (await client.get(preview_url, headers=owner)).json()["data"]
            monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_DPI", 200)
            monkeypatch.setattr(knowledge_jobs, "parse_knowledge_isolated", reparse)
            monkeypatch.setattr(knowledge_jobs, "chunk_knowledge", fail)
            queued = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": uploaded["revision"],
                },
            )
            assert queued.status_code == 202
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, queued.json()["data"]["job_id"])
                version = await db.scalar(select(KnowledgeDocumentVersion))
                assert job.state == KnowledgeJobState.FAILED
                assert version.processing_metadata["parser"] == "candidate-parse"
            current = (await client.get(preview_url, headers=owner)).json()["data"]
            assert current["index_id"] == previous["index_id"]
            assert current["processing_metadata"] == previous["processing_metadata"]
            assert current["items"] == previous["items"]

    asyncio.run(exercise())
