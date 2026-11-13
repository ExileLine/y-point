# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 09:33:46
# @Author  : yangyuexiong
# @File    : test_knowledge_pdf_acceptance.py

"""使用真实本地 OCR 验收上传、发布、检索及原页证据的完整链路。"""

import asyncio
import io
import json
import shutil
import subprocess

import pytest
from PIL import Image, ImageChops
from sqlalchemy import select

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, KnowledgeJobState, KnowledgePdfExtraction
from app.models.knowledge import KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeJob
from app.services.knowledge_pdf import _normalize_pdf_line
from scripts.generate_knowledge_pdf_fixtures import make_pdf_fixture
from tests.test_knowledge import _create_kb, _drain, _search, _upload
from tests.test_m51_governance import _database, _http_platform


def _require_real_ocr() -> None:
    """检查真实 OCR 引擎及完整语言包，缺失环境不得冒充验收通过。"""
    command = shutil.which("tesseract")
    if command is None:
        pytest.skip("真实 OCR 验收需要 Tesseract")
    result = subprocess.run([command, "--list-langs"], capture_output=True, text=True, timeout=5, check=True)
    if not {"chi_sim", "eng"}.issubset(result.stdout.splitlines()):
        pytest.skip("真实 OCR 验收需要 chi_sim 和 eng 语言包")


@pytest.mark.parametrize(
    "kind,query,expected_pages,expected_ocr",
    [
        ("scan", "Hotel", 1, 1),
        ("chinese-scan", "五千元", 1, 1),
        ("mixed", "Hotel", 3, 1),
    ],
)
def test_real_pdf_upload_index_search_citation_and_page(tmp_path, monkeypatch, kind: str, query: str, expected_pages: int, expected_ocr: int) -> None:
    """真实扫描与混合 PDF 经正式接口及任务发布后，业务词能检索且引用坐标覆盖原图。"""
    _require_real_ocr()
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_COMMAND", "tesseract")
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_LANGUAGES", "chi_sim+eng")

    async def exercise() -> None:
        """保留真实解析及 OCR，仅隔离业务数据库和文件目录，不配置远程模型。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            kb = await _create_kb(client, owner, processing_mode="original")
            uploaded = await _upload(client, owner, kb, f"{kind}.pdf", make_pdf_fixture(kind))
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED, job.failure_code
                assert job.processing_call_count == job.processing_reserved_tokens == job.reserved_tokens == 0
                version = await db.scalar(select(KnowledgeDocumentVersion))
                chunks = (await db.scalars(select(KnowledgeChunk))).all()
                assert chunks and all(chunk.content == version.extracted_text[chunk.start_offset : chunk.end_offset] for chunk in chunks)
                assert all(not chunk.knowledge_notes for chunk in chunks)
            document_id = uploaded["document"]["id"]
            preview_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}/processing-preview"
            preview = await client.get(preview_url, headers=owner)
            assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
            metadata = preview.json()["data"]["processing_metadata"]
            assert len(metadata["pages"]) == expected_pages and metadata["ocr_pages"] == expected_ocr
            assert metadata["blank_pages"] == (1 if kind == "mixed" else 0)
            hits = (await _search(client, owner, kb["id"], query))["hits"]
            assert hits, f"真实 OCR 的业务检索词未命中：{query}；提取结果：{version.extracted_text}"
            if kind == "chinese-scan":
                for term in ("十五个工作日", "财务部"):
                    assert (await _search(client, owner, kb["id"], term))["hits"], f"真实 OCR 业务词未命中：{term}"
            if kind == "mixed":
                assert {chunk.page for chunk in chunks} == {1, 2}
                # 相同正文允许检索去重；扫描页仍须保留独立、可读的固定引用。
                ocr_chunk = next(chunk for chunk in chunks if chunk.page == 2)
                ocr_citation = await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{ocr_chunk.id}", headers=owner)
                assert ocr_citation.status_code == 200
                hit = ocr_citation.json()["data"]
            else:
                hit = hits[0]
            assert hit["source_locator"]["extraction_method"] == KnowledgePdfExtraction.OCR
            assert hit["source_locator"]["review_required"] is True
            citation_url = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}"
            citation = await client.get(citation_url, headers=owner)
            assert citation.status_code == 200 and citation.json()["data"]["content"] == hit["content"]
            image_url = f"{citation_url}/page-image"
            image = await client.get(image_url, headers=owner)
            assert image.status_code == 200 and image.headers["content-type"].startswith("image/png")
            assert image.headers["cache-control"] == "no-store"
            source = hit["source_locator"]
            with Image.open(io.BytesIO(image.content)) as original:
                assert abs(original.width / original.height - source["page_width"] / source["page_height"]) < 0.002
                x0, top, x1, bottom = source["bbox"]
                region = original.crop(
                    (
                        x0 * original.width / source["page_width"],
                        top * original.height / source["page_height"],
                        x1 * original.width / source["page_width"],
                        bottom * original.height / source["page_height"],
                    ),
                ).convert("RGB")
                assert ImageChops.difference(region, Image.new("RGB", region.size, "white")).getbbox() is not None
            assert (await client.get(preview_url, headers=other)).status_code == 404
            assert (await client.get(citation_url, headers=other)).status_code == 404
            assert (await client.get(image_url, headers=other)).status_code == 404

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "text,expected",
    [
        ("住宿 费用 上 限 为 五 千 元 。", "住宿费用上限为五千元 。"),
        ("报销 申请 须 在 十 五 个 工作 日 内 提交 。", "报销申请须在十五个工作日内提交 。"),
        ("Hotel allowance: 5000 CNY per trip.", "Hotel allowance: 5000 CNY per trip."),
        ("住宿 费用 5000 CNY 每 次", "住宿费用 5000 CNY 每次"),
        ("产品 A B 规格 12 34 元", "产品 A B 规格 12 34 元"),
        ("第 一 行\n第 二 行", "第一行\n第二行"),
        ("甲\t乙", "甲乙"),
        ("甲 ； 乙", "甲 ； 乙"),
    ],
)
def test_pdf_chinese_spacing_preserves_numbers_english_and_lines(text: str, expected: str) -> None:
    """只清理中文分词空格，不把数字、英文词、标点或不同证据行拼成新含义。"""
    assert _normalize_pdf_line(text) == expected


def test_real_pdf_mode_failure_then_force_ocr_recovery(tmp_path, monkeypatch) -> None:
    """真实扫描文件更改提取策略后，失败保留旧代及原页，改为强制 OCR 可重新发布。"""
    _require_real_ocr()
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_ENABLED", True)
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_COMMAND", "tesseract")
    monkeypatch.setattr(get_config(), "KNOWLEDGE_PDF_OCR_LANGUAGES", "chi_sim+eng")

    async def exercise() -> None:
        """同一文件依次发布自动提取、失败文字层、成功强制 OCR 三个版本。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner, processing_mode="original")
            content = make_pdf_fixture("scan")
            uploaded = await _upload(client, owner, kb, "scan.pdf", content)
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "5000"))["hits"][0]
            citation_url = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}"
            document_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{uploaded['document']['id']}"
            for mode, version, state in (("text", 2, KnowledgeJobState.FAILED), ("ocr", 3, KnowledgeJobState.COMPLETED)):
                updated = await client.put(
                    document_url,
                    headers=owner,
                    data={
                        "expected_revision": str(kb["revision"]),
                        "processing_options": json.dumps(
                            {
                                "pdf_mode": mode,
                            },
                        ),
                    },
                    files={
                        "file": ("scan.pdf", content),
                    },
                )
                assert updated.status_code == 202, updated.text
                result = updated.json()["data"]
                kb["revision"] = result["revision"]
                assert result["document"]["current_version"] == version
                await _drain(sessions)
                async with sessions() as db:
                    job = await db.get(KnowledgeJob, result["job_id"])
                    assert job.state == state
                    if mode == "text":
                        assert job.failure_code == KnowledgeFailure.OCR_REQUIRED
                preview = (await client.get(f"{document_url}/processing-preview", headers=owner)).json()["data"]
                assert preview["indexed_version"] == (1 if mode == "text" else 3)
                assert preview["processing_metadata"]["ocr_pages"] == 1
                matches = (await _search(client, owner, kb["id"], "5000"))["hits"]
                assert matches and matches[0]["document_version"] == (1 if mode == "text" else 3)
                assert (await client.get(citation_url, headers=owner)).status_code == 200
                assert (await client.get(f"{citation_url}/page-image", headers=owner)).status_code == 200
            deleted = await client.delete(
                document_url,
                headers=owner,
                params={
                    "expected_revision": kb["revision"],
                },
            )
            assert deleted.status_code == 200
            assert not (await _search(client, owner, kb["id"], "5000"))["hits"]
            assert (await client.get(citation_url, headers=owner)).status_code == 404
            assert (await client.get(f"{citation_url}/page-image", headers=owner)).status_code == 404

    asyncio.run(exercise())
