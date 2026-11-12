# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 15:00:37
# @Author  : yangyuexiong
# @File    : test_knowledge_processing.py

"""通过真实文件、正式路由及 worker 验证表格证据、加工与安全发布。"""

import asyncio
import io
import json
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from openpyxl import Workbook
from openpyxl.worksheet.table import Table
from sqlalchemy import func, select

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, KnowledgeJobState
from app.models.knowledge import KnowledgeChunk, KnowledgeDocument, KnowledgeDocumentVersion, KnowledgeEnrichmentCache, KnowledgeJob
from app.models.model_catalog import ModelCatalog
from app.schemas.knowledge import KnowledgeCreateIn, KnowledgeDocumentOptions
from app.services import knowledge_enrichment
from app.services.knowledge_jobs import process_next_knowledge_job
from app.services.knowledge_parser import KnowledgeProcessingError, chunk_knowledge, parse_knowledge, parse_knowledge_isolated
from app.services.knowledge_tables import XLSX_MEDIA_TYPE
from tests.test_knowledge import _create_kb, _drain, _search, _seed_models, _upload
from tests.test_m51_governance import _database, _http_platform


def _workbook() -> bytes:
    """生成含隐藏内容、多级表头、公式和多个数据区域的真实 XLSX。"""
    book = Workbook()
    sheet = book.active
    sheet.title = "报价"
    sheet.append(["供应商", "报价", "报价", "内部备注"])
    sheet.append(["名称", "单价（元）", "数量（吨）", "不公开"])
    sheet.merge_cells("B1:C1")
    sheet.append(["甲供应商", 3800, 50, "隐藏列机密"])
    sheet.append(["隐藏行机密", 9999, 1, "备注"])
    sheet.row_dimensions[4].hidden = True
    sheet.column_dimensions["D"].hidden = True
    sheet.append(["乙供应商", "=B3*2", 20, "备注"])
    sheet = book.create_sheet("其他")
    sheet.append(["名称", "值"])
    sheet.append(["不应选择的工作表", "数据"])
    sheet = book.create_sheet("机密")
    sheet.sheet_state = "hidden"
    sheet.append(["隐藏工作表机密"])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_xlsx_record_structure_merges_formulas_and_source() -> None:
    """多级合并表头沿列传播，隐藏内容不进入证据，公式只展示表达式与缓存状态。"""
    parsed = parse_knowledge(
        _workbook(),
        XLSX_MEDIA_TYPE,
        {
            "sheet_names": ["报价"],
            "header_rows": 2,
        },
    )
    assert "报价 / 单价（元）[B]" in parsed.text
    assert "报价 / 数量（吨）[C]" in parsed.text
    assert "3800" in parsed.text and "50" in parsed.text
    assert "=B3*2" in parsed.text and "未提供，未计算" in parsed.text
    assert "机密" not in parsed.text and "不应选择" not in parsed.text
    pieces = chunk_knowledge(parsed, 200, 0)
    assert sum(piece["content"].count("供应商 / 名称[A]=") for piece in pieces) == 2
    for piece in pieces:
        assert piece["content"] == parsed.text[piece["start_offset"] : piece["end_offset"]]
        assert piece["source_locator"]["sheet"] == "报价"
        assert piece["source_locator"]["columns"] == ["A", "B", "C"]
        assert piece["source_locator"]["header_range"] == "A1:D2"
    assert pieces[0]["source_locator"]["range"] == "A3:C5"


def test_excel_table_regions_keep_independent_headings() -> None:
    """正式 Excel Table 各自保留表头和区域，不将旁侧表格拼成同一记录。"""
    book = Workbook()
    sheet = book.active
    for row in [["名称", "额度", None, "项目", "数量"], ["甲", 10, None, "乙", 20]]:
        sheet.append(row)
    sheet.add_table(Table(displayName="LeftTable", ref="A1:B2"))
    sheet.add_table(Table(displayName="RightTable", ref="D1:E2"))
    output = io.BytesIO()
    book.save(output)
    parsed = parse_knowledge(output.getvalue(), XLSX_MEDIA_TYPE)
    pieces = chunk_knowledge(parsed, 2000, 0)
    assert len(pieces) == 2
    assert pieces[0]["source_locator"]["table_range"] == "A1:B2"
    assert pieces[1]["source_locator"]["table_range"] == "D1:E2"
    assert "项目[D]" in pieces[1]["content"]


def test_csv_multiline_records_have_physical_source_rows() -> None:
    """CSV 的引号、换行、零值和重复字段保留原意，记录引用使用真实行范围。"""
    content = '\ufeff名称,备注,金额,金额\r\n甲,"第一行\n第二行",0,12.50\r\n乙,普通,8,9\r\n'.encode()
    parsed = asyncio.run(parse_knowledge_isolated(content, "text/csv"))
    pieces = chunk_knowledge(parsed, 200, 0)
    assert '金额[C]="0"' in pieces[0]["content"]
    assert '金额[D]="12.50"' in pieces[0]["content"]
    assert "第一行\\n第二行" in pieces[0]["content"]
    assert pieces[0]["source_locator"]["row_start"] == 2
    assert pieces[0]["source_locator"]["row_end"] >= 3
    assert sum(piece["content"].count("名称[A]=") for piece in pieces) == 2


@pytest.mark.parametrize(
    "settings",
    [
        {
            "sheet_names": ["不存在"],
        },
        {
            "sheet_names": ["机密"],
        },
        {
            "header_rows": 5,
        },
    ],
)
def test_invalid_table_selection_does_not_silently_index(settings: dict) -> None:
    """未知或隐藏工作表以及无数据表头配置必须明确失败，不悄悄发布其他内容。"""
    with pytest.raises(KnowledgeProcessingError):
        parse_knowledge(_workbook(), XLSX_MEDIA_TYPE, settings)


@pytest.mark.parametrize("content", ["名称,金额\n甲,1,多余字段\n", "名称,说明\n甲," + "长" * 2100 + "\n"])
def test_csv_ragged_or_oversized_records_fail_explicitly(content: str) -> None:
    """字段数不一致及过长记录拒绝处理，不截断或丢弃尾部字段。"""
    with pytest.raises(KnowledgeProcessingError):
        parse_knowledge(content.encode(), "text/csv")


@pytest.mark.parametrize(
    "response",
    [
        '{"notes": [{"position": 99, "text": "越界"}]}',
        '{"notes": [{"position": 0, "text": "重复"}, {"position": 0, "text": "重复"}]}',
        '{"notes": []}',
        '{"notes": [{"position": true, "text": "类型不符"}]}',
        "```json\n{}\n```",
    ],
)
def test_generated_knowledge_must_match_exact_evidence(response: str) -> None:
    """模型生成结果必须覆盖当前批次且引用唯一已知位置，未知结构不进入索引。"""
    with pytest.raises(KnowledgeProcessingError) as failure:
        knowledge_enrichment.validate_knowledge_notes(response, [0])
    assert failure.value.code == KnowledgeFailure.INVALID_KNOWLEDGE


class TableExplanationModel:
    """无工具的确定性加工模型，帮助检索原文未包含的业务解释词。"""

    def __init__(self, mode: str = "ok") -> None:
        """保存响应模式及调用次数，便于验证缓存和失败计量。"""
        self.mode, self.calls = mode, 0

    async def ainvoke(self, messages):
        """仅根据传入证据位置返回结构化解释，使用真实模型消息用量契约。"""
        self.calls += 1
        evidence = json.loads(messages[1].content)
        content = (
            json.dumps(
                {
                    "notes": [
                        {
                            "position": row["position"],
                            "text": "采购定价依据说明，供应商字段表示报价主体，单价与数量应结合原始字段单位核对。",
                        }
                        for row in evidence
                    ],
                },
                ensure_ascii=False,
            )
            if self.mode != "invalid"
            else '{"notes":[]}'
        )
        return AIMessage(
            content=content,
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
            },
            response_metadata={
                "finish_reason": "length" if self.mode == "truncated" else "stop",
            },
        )


async def _configure_model(sessions) -> None:
    """设置加工价格，验证加工和嵌入独立记账且币种不会混合。"""
    await _seed_models(sessions)
    async with sessions.begin() as db:
        model = await db.get(ModelCatalog, 1)
        model.input_price = Decimal("1")
        model.output_price = Decimal("2")


def test_table_http_worker_enrichment_retrieval_preview_cache_and_permissions(tmp_path, monkeypatch) -> None:
    """完整 XLSX 链路保留原文、定位和生成解释，重建复用缓存，预览和引用逐次鉴权。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    model = TableExplanationModel()
    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: model)

    async def exercise() -> None:
        """使用正式 multipart、任务、检索、预览和引用路由验证持久化行为。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, stranger = headers
            await _configure_model(sessions)
            kb = await _create_kb(client, owner, processing_mode="enhanced", processing_model_id=1)
            response = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                headers=owner,
                data={
                    "expected_revision": kb["revision"],
                    "processing_options": json.dumps(
                        {
                            "sheet_names": ["报价"],
                            "header_rows": 2,
                        },
                    ),
                },
                files={
                    "file": ("采购.xlsx", _workbook()),
                },
            )
            assert response.status_code == 202, response.text
            upload = response.json()["data"]
            kb["revision"] = upload["revision"]
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "采购定价依据"))["hits"][0]
            assert "采购定价依据" not in hit["content"] and "采购定价依据" in hit["knowledge_notes"]
            assert hit["source_locator"]["sheet"] == "报价"
            async with sessions() as db:
                version = await db.scalar(select(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.document_id == upload["document"]["id"]))
                assert hit["content"] == version.extracted_text[hit["start_offset"] : hit["end_offset"]]
                job = await db.get(KnowledgeJob, upload["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED
                assert job.processing_call_count == 1 and job.processing_tokens == 120
                assert job.embedding_tokens == 0 and job.processing_estimated_cost > 0
                assert job.processing_usage[0]["currency"] == "USD"
                assert "3800" not in json.dumps(job.processing_usage)
            preview_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{upload['document']['id']}/processing-preview"
            preview = await client.get(preview_url + "?limit=1", headers=owner)
            assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
            assert preview.json()["data"]["items"][0]["source_locator"]["sheet"] == "报价"
            assert (await client.get(preview_url, headers=stranger)).status_code == 404
            reference = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}"
            assert (await client.get(reference, headers=owner)).json()["data"] == {key: value for key, value in hit.items() if key != "score"}
            assert (await client.get(reference, headers=stranger)).status_code == 404
            rebuilt = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            assert rebuilt.status_code == 202, rebuilt.text
            await _drain(sessions)
            assert model.calls == 1
            async with sessions() as db:
                latest = await db.get(KnowledgeJob, rebuilt.json()["data"]["job_id"])
                assert latest.processing_call_count == 0 and latest.processing_tokens is None
            deleted = await client.delete(
                f"/api/v1/knowledge-bases/{kb['id']}/documents/{upload['document']['id']}?expected_revision={rebuilt.json()['data']['revision']}",
                headers=owner,
            )
            assert deleted.status_code == 200, deleted.text
            assert (await client.get(preview_url, headers=owner)).status_code == 404
            assert (await client.get(reference, headers=owner)).status_code == 404

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["invalid", "truncated", "budget", "identity"])
def test_processing_failure_preserves_previous_index_and_counts_usage(tmp_path, monkeypatch, mode: str) -> None:
    """结构错误、截断、预算及模型身份变化都不能发布半成品或绕过失败计量。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    model = TableExplanationModel(mode)
    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: model)

    async def exercise() -> None:
        """先发布可用原文，再以失败加工任务验证活动代保持稳定。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _configure_model(sessions)
            kb = await _create_kb(client, owner)
            await _upload(client, owner, kb, "原有.txt", "原有规则继续可用".encode())
            await _drain(sessions)
            original = (await client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=owner)).json()["data"]
            values = {key: original[key] for key in KnowledgeCreateIn.model_fields}
            values.update(
                {
                    "processing_mode": "enhanced",
                    "processing_model_id": 1,
                    "processing_token_budget": 1 if mode == "budget" else 100000,
                    "expected_revision": original["revision"],
                },
            )
            response = await client.put(f"/api/v1/knowledge-bases/{kb['id']}", headers=owner, json=values)
            assert response.status_code == 200, response.text
            kb["revision"] = response.json()["data"]["knowledge_base"]["revision"]
            uploaded = await _upload(client, owner, kb, "报价.csv", "供应商,单价\n甲,3800\n".encode())
            if mode == "identity":
                async with sessions.begin() as db:
                    catalog = await db.get(ModelCatalog, 1)
                    catalog.external_id = "changed-model"
            await _drain(sessions)
            job = (await client.get(f"/api/v1/knowledge-bases/jobs/{uploaded['job_id']}", headers=owner)).json()["data"]
            assert job["state"] == "failed"
            if mode in {"invalid", "truncated"}:
                assert job["failure_code"] == "invalid_knowledge"
                assert job["processing_tokens"] == 120 and job["processing_call_count"] == 1
            else:
                assert job["failure_code"] == ("budget_exceeded" if mode == "budget" else "model_unavailable")
                assert model.calls == 0
            current = (await client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=owner)).json()["data"]
            assert current["active_index_id"] == original["active_index_id"]
            assert (await _search(client, owner, kb["id"], "原有规则"))["hits"]
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(KnowledgeEnrichmentCache)) == 0

    asyncio.run(exercise())


def test_processing_configuration_schema_and_csv_scope(tmp_path, monkeypatch) -> None:
    """增强模式要求模型，CSV 仅开放给知识库，错误选项不会创建任务或文件。"""
    with pytest.raises(ValueError):
        KnowledgeCreateIn(name="增强", processing_mode="enhanced")
    with pytest.raises(ValueError):
        KnowledgeDocumentOptions(sheet_names=["报价", "报价"])
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """原文模式上传 CSV 不产生模型调用，非法覆盖配置在接收正文前拒绝。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner)
            assert kb["processing_token_budget"] is None and kb["embedding_token_budget"] is None
            uploaded = await _upload(client, owner, kb, "记录.csv", "名称,金额\n甲,0\n".encode())
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "金额"))["hits"][0]
            assert '金额[B]="0"' in hit["content"] and hit["knowledge_notes"] is None
            async with sessions.begin() as db:
                document = await db.get(KnowledgeDocument, uploaded["document"]["id"])
                document.processing_options = {}
            duplicate = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                headers=owner,
                data={
                    "expected_revision": kb["revision"],
                    "processing_options": "{}",
                },
                files={
                    "file": ("记录.csv", "名称,金额\n甲,0\n".encode()),
                },
            )
            assert duplicate.status_code == 202, duplicate.text
            assert duplicate.json()["data"]["deduplicated"] is True
            assert duplicate.json()["data"]["revision"] == kb["revision"]
            invalid = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                headers=owner,
                data={
                    "expected_revision": kb["revision"],
                    "processing_options": '{"header_rows":0}',
                },
                files={
                    "file": ("非法.csv", b"a,b\n1,2\n"),
                },
            )
            assert invalid.status_code == 422
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.processing_call_count == 0

    asyncio.run(exercise())


def test_knowledge_processing_migration_roundtrip(tmp_path: Path) -> None:
    """隔离 SQLite 验证新增结构、回退、重升及 ORM 差异检查，不触碰开发库。"""
    database = tmp_path / "knowledge-processing.db"
    environment = os.environ.copy()
    environment.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
        },
    )
    root = Path(__file__).resolve().parents[1]

    def migrate(action: str, revision: str) -> None:
        """仅操作隔离测试数据库，错误输出保留用于定位迁移差异。"""
        result = subprocess.run([sys.executable, "-m", "alembic", action, revision], cwd=root, env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "050")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO yp_knowledge_base
            (id, owner_id, name, revision, search_mode, chunk_size, chunk_overlap, embedding_token_budget,
             processing_token_budget, create_time, update_time, create_timestamp, update_timestamp)
            VALUES (1, 1, '迁移保留数值', 1, 'keyword', 800, 100, 345678, 123456,
                    '2026-11-12 00:00:00', '2026-11-12 00:00:00', 0, 0)""",
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT embedding_token_budget, processing_token_budget FROM yp_knowledge_base").fetchone() == (345678, 123456)
        columns = {row[1]: row for row in connection.execute("PRAGMA table_info(yp_knowledge_base)")}
        assert columns["embedding_token_budget"][3] == columns["processing_token_budget"][3] == 0
        assert columns["processing_token_budget"][4] is None
        connection.execute("UPDATE yp_knowledge_base SET embedding_token_budget=NULL, processing_token_budget=NULL")
    migrate("downgrade", "050")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT embedding_token_budget, processing_token_budget FROM yp_knowledge_base").fetchone() == (200000, 100000)
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA foreign_key_list(yp_knowledge_enrichment_cache)").fetchall() == []
        assert "processing_options" in [row[1] for row in connection.execute("PRAGMA table_info(yp_knowledge_document)")]
    migrate("downgrade", "049")
    migrate("upgrade", "head")
    result = subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=root, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_processing_retry_reuses_successful_batches_and_preserves_reservation(tmp_path, monkeypatch) -> None:
    """中途失败后重试只补齐未完成批次，成功缓存复用且失败请求仍占预留预算。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    class InterruptedModel(TableExplanationModel):
        """第二批模拟断连，其余批次返回已验证的解释。"""

        async def ainvoke(self, messages):
            """只中断一次，重试时不重新发出第一批请求。"""
            if self.calls == 1:
                self.calls += 1
                raise ConnectionError("private-provider-and-document-must-not-leak")
            return await super().ainvoke(messages)

    model = InterruptedModel()
    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: model)

    async def exercise() -> None:
        """以较小片段构造多批证据，通过正式重试接口恢复。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _configure_model(sessions)
            kb = await _create_kb(client, owner, processing_mode="enhanced", processing_model_id=1, chunk_size=200, chunk_overlap=0)
            text = "供应商,单价,说明\n" + "\n".join(f"供应商{number},{3800 + number}," + "完整记录" * 12 for number in range(15))
            uploaded = await _upload(client, owner, kb, "多批.csv", text.encode())
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.FAILED and job.processing_call_count == 2
                reserved = job.processing_reserved_tokens
                assert await db.scalar(select(func.count()).select_from(KnowledgeEnrichmentCache)) == 1
                assert "private-provider" not in json.dumps(job.processing_usage)
            response = await client.post(f"/api/v1/knowledge-bases/jobs/{uploaded['job_id']}/retry?expected_attempts=1", headers=owner)
            assert response.status_code == 200, response.text
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED
                assert job.processing_reserved_tokens > reserved and job.attempts == 2
                cache_count = await db.scalar(select(func.count()).select_from(KnowledgeEnrichmentCache))
                assert job.processing_call_count == cache_count + 1
            assert (await _search(client, owner, kb["id"], "采购定价依据"))["hits"]

    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["cancel", "disable-owner", "supersede"])
def test_processing_wait_obeys_cancel_user_and_revision(tmp_path, monkeypatch, action: str) -> None:
    """模型等待期间的取消、停用和新代都能终止任务，不保存半成品或泄露名额。"""
    from app.models.admin import Admin
    from app.models.tool_execution import ToolExecutionLease

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """真实 watcher 发现状态变化后取消模型等待，失败代无片段和加工缓存。"""
        started = asyncio.Event()

        class WaitingModel:
            """永远等待的加工模型，用于验证 watcher 不依赖远端返回。"""

            async def ainvoke(self, messages):
                """通知外部已发起调用，随后等待取消。"""
                started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: WaitingModel())
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _configure_model(sessions)
            kb = await _create_kb(client, owner, processing_mode="enhanced", processing_model_id=1)
            uploaded = await _upload(client, owner, kb, "等待.csv", "名称,单价\n甲,3800\n".encode())
            task = asyncio.create_task(process_next_knowledge_job(sessions))
            await asyncio.wait_for(started.wait(), timeout=10)
            try:
                if action == "cancel":
                    response = await client.post(f"/api/v1/knowledge-bases/jobs/{uploaded['job_id']}/cancel", headers=owner)
                    assert response.status_code == 200
                elif action == "disable-owner":
                    async with sessions.begin() as db:
                        actor = await db.get(Admin, 1)
                        actor.status = 0
                else:
                    response = await client.post(
                        f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                        headers=owner,
                        json={
                            "expected_revision": kb["revision"],
                        },
                    )
                    assert response.status_code == 202, response.text
                await asyncio.wait_for(task, timeout=8)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.CANCELLED
                assert job.processing_call_count == 1 and job.processing_tokens is None
                assert await db.scalar(select(func.count()).select_from(KnowledgeEnrichmentCache)) == 0
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0
                assert await db.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0

    asyncio.run(exercise())


def test_xlsx_grouped_hidden_columns_are_excluded() -> None:
    """分组隐藏整段列时，每个被覆盖列都须排除而不只检查首列维度。"""
    book = Workbook()
    sheet = book.active
    sheet.append(["公开名称", "内部单价", "内部说明", "公开分类"])
    sheet.append(["甲", "隐藏组金额", "隐藏组备注", "材料"])
    sheet.column_dimensions.group("B", "C", hidden=True)
    stream = io.BytesIO()
    book.save(stream)
    parsed = parse_knowledge(stream.getvalue(), XLSX_MEDIA_TYPE)
    assert "隐藏组" not in parsed.text
    assert parsed.blocks[0]["source_locator"]["columns"] == ["A", "D"]
    assert parsed.blocks[0]["source_locator"]["hidden_excluded"]


def test_processing_batches_fit_small_context_without_splitting_evidence() -> None:
    """较小上下文自动拆批，单条证据也放不下时明确失败且不截断。"""
    from types import SimpleNamespace

    chunks = [SimpleNamespace(position=number, content="原始记录" * 180, source_locator=None) for number in range(3)]
    single = list(
        knowledge_enrichment._processing_batches(
            chunks[:1],
            {
                "context_window": None,
            },
        )
    )[0]
    limit = single[3] + knowledge_enrichment.MAX_ENRICHMENT_OUTPUT + 512
    batches = list(
        knowledge_enrichment._processing_batches(
            chunks,
            {
                "context_window": limit,
            },
        )
    )
    assert [len(batch[0]) for batch in batches] == [1, 1, 1]
    assert [batch[1][0]["content"] for batch in batches] == [chunk.content for chunk in chunks]
    with pytest.raises(KnowledgeProcessingError) as error:
        list(
            knowledge_enrichment._processing_batches(
                chunks,
                {
                    "context_window": limit - 1,
                },
            )
        )
    assert error.value.code == KnowledgeFailure.SIZE_LIMIT


def test_hybrid_embeds_explanations_but_cites_original_evidence(tmp_path, monkeypatch) -> None:
    """混合检索向量包含解释，引用仍是原始偏移，标准供应商不接收不支持的思考参数。"""
    from app.services import knowledge as knowledge_service, knowledge_jobs
    from app.services.embedding_adapter import EmbeddingResult
    from app.services.model_adapter import thinking_request_options

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    explanations = TableExplanationModel()
    embedded = []

    def build(model, provider, **options):
        """使用正式参数校验避免模拟模型掩盖供应商不兼容。"""
        thinking_request_options(provider.kind, options["thinking_mode"])
        return explanations

    async def embed(model, provider, texts, **options):
        """记录正式构建输入，使用固定单位向量隔离外部嵌入服务。"""
        embedded.extend(texts)
        return EmbeddingResult([[1.0, 0.0] for _ in texts], len(texts), None)

    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", build)
    monkeypatch.setattr(knowledge_jobs, "embed_texts", embed)
    monkeypatch.setattr(knowledge_service, "embed_texts", embed)

    async def exercise() -> None:
        """完整索引后执行真实检索，验证两类模型计量与证据边界。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _configure_model(sessions)
            kb = await _create_kb(client, owner, search_mode="hybrid", embedding_model_id=2, processing_mode="enhanced", processing_model_id=1)
            await _upload(client, owner, kb, "报价.csv", "供应商,单价\n甲,3800\n".encode())
            await _drain(sessions)
            assert embedded and all("检索解释（模型生成）" in text and "3800" in text for text in embedded)
            hit = (await _search(client, owner, kb["id"], "采购定价依据"))["hits"][0]
            assert "采购定价依据" not in hit["content"] and "采购定价依据" in hit["knowledge_notes"]
            async with sessions() as db:
                version = await db.scalar(select(KnowledgeDocumentVersion))
                assert hit["content"] == version.extracted_text[hit["start_offset"] : hit["end_offset"]]
                job = await db.scalar(select(KnowledgeJob))
                assert job.embedding_tokens > 0 and job.processing_tokens == 120

    asyncio.run(exercise())


@pytest.mark.parametrize("change", ["during-processing", "during-embedding", "reported-budget"])
def test_processing_final_publication_rechecks_identity_and_actual_usage(tmp_path, monkeypatch, change: str) -> None:
    """加工或嵌入等待时修改模型、实际用量超预算，均禁止发布已失效构建。"""
    from app.services import knowledge_jobs
    from app.services.embedding_adapter import EmbeddingResult

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """在外部调用返回边界变更模型，使用正式 worker 观察最终失败类别。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            await _configure_model(sessions)

            async def invalidate() -> None:
                """模拟管理员在等待期间切换供应商模型身份。"""
                async with sessions.begin() as db:
                    model = await db.get(ModelCatalog, 1)
                    model.external_id = "changed-while-awaiting"

            class ChangedModel(TableExplanationModel):
                """在成功响应后改变配置或返回超出预留的实际用量。"""

                async def ainvoke(self, messages):
                    """原始证据解释有效，但构建身份或预算已不再符合快照。"""
                    response = await super().ainvoke(messages)
                    if change == "during-processing":
                        await invalidate()
                    elif change == "reported-budget":
                        response.usage_metadata = {
                            "input_tokens": 100000,
                            "output_tokens": 20,
                            "total_tokens": 100020,
                        }
                    return response

            async def embed(model, provider, texts, **options):
                """模拟向量返回时加工模型已改变，覆盖发布前最后一次身份校验。"""
                if change == "during-embedding":
                    await invalidate()
                return EmbeddingResult([[1.0, 0.0] for _ in texts], len(texts), None)

            monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: ChangedModel())
            monkeypatch.setattr(knowledge_jobs, "embed_texts", embed)
            kb = await _create_kb(
                client,
                headers[0],
                search_mode="hybrid",
                embedding_model_id=2,
                processing_mode="enhanced",
                processing_model_id=1,
                processing_token_budget=100000,
            )
            uploaded = await _upload(client, headers[0], kb, "记录.csv", "名称,金额\n甲,3800\n".encode())
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.FAILED
                assert job.failure_code == (KnowledgeFailure.BUDGET_EXCEEDED if change == "reported-budget" else KnowledgeFailure.MODEL_UNAVAILABLE)
                assert job.processing_call_count == 1 and job.processing_tokens is not None
                assert await db.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0
                if change == "during-processing":
                    assert await db.scalar(select(func.count()).select_from(KnowledgeEnrichmentCache)) == 0

    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["processing_token_budget", "embedding_token_budget"])
def test_optional_knowledge_budget_validation(field: str) -> None:
    """预算允许省略和显式空值，保留有限额度的正整数范围校验。"""
    assert getattr(KnowledgeCreateIn(name="不限累计用量"), field) is None
    empty = {
        field: None,
    }
    finite = {
        field: 1,
    }
    assert getattr(KnowledgeCreateIn(name="不限累计用量", **empty), field) is None
    assert getattr(KnowledgeCreateIn(name="有限额度", **finite), field) == 1
    for invalid in (0, -1, 2000001):
        with pytest.raises(ValueError):
            values = {
                field: invalid,
            }
            KnowledgeCreateIn(name="错误额度", **values)


@pytest.mark.parametrize(
    "scenario",
    ["unlimited", "processing-reserved-cost", "processing-actual-cost", "embedding-reserved-cost", "embedding-actual-cost"],
)
def test_unlimited_knowledge_tokens_keep_usage_and_independent_cost_limits(tmp_path, monkeypatch, scenario: str) -> None:
    """通过正式路由及 worker 验证空额度超过旧默认仍发布，独立费用在请求前后均拦截。"""
    from app.models.knowledge import KnowledgeBase, KnowledgeIndex
    from app.services import knowledge_jobs
    from app.services.embedding_adapter import EmbeddingResult

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    class HighUsageModel(TableExplanationModel):
        """用确定性响应模拟累计用量超过旧默认上限，不调用真实供应商。"""

        async def ainvoke(self, messages):
            """保留有效解释，并报告超出旧十万额度的加工用量。"""
            response = await super().ainvoke(messages)
            response.usage_metadata = {
                "input_tokens": 120000,
                "output_tokens": 20,
                "total_tokens": 120020,
            }
            return response

    model, embedded = HighUsageModel(), []

    async def embed(model, provider, texts, **options):
        """报告超过旧二十万额度的嵌入用量，提供独立费用以验证发布边界。"""
        embedded.extend(texts)
        return EmbeddingResult([[1.0, 0.0] for _ in texts], 300000, Decimal("0.300000"))

    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: model)
    monkeypatch.setattr(knowledge_jobs, "embed_texts", embed)

    async def exercise() -> None:
        """先保存有限额度再清空，验证数据库、任务快照与执行阶段的一致语义。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _configure_model(sessions)
            async with sessions.begin() as db:
                embedding_model = await db.get(ModelCatalog, 2)
                embedding_model.input_price = Decimal("1")
            kb = await _create_kb(
                client,
                owner,
                search_mode="hybrid",
                embedding_model_id=2,
                processing_mode="enhanced",
                processing_model_id=1,
                processing_token_budget=100000,
                embedding_token_budget=200000,
            )
            values = {key: kb[key] for key in KnowledgeCreateIn.model_fields}
            processing_cost = "0.000001" if scenario == "processing-reserved-cost" else "0.010000" if scenario == "processing-actual-cost" else None
            embedding_cost = "0.000001" if scenario == "embedding-reserved-cost" else "0.010000" if scenario == "embedding-actual-cost" else None
            values.update(
                {
                    "processing_token_budget": None,
                    "embedding_token_budget": None,
                    "processing_cost_budget": processing_cost,
                    "embedding_cost_budget": embedding_cost,
                    "expected_revision": kb["revision"],
                },
            )
            response = await client.put(f"/api/v1/knowledge-bases/{kb['id']}", headers=owner, json=values)
            assert response.status_code == 200, response.text
            kb = response.json()["data"]["knowledge_base"]
            assert kb["processing_token_budget"] is None and kb["embedding_token_budget"] is None
            await _drain(sessions)
            uploaded = await _upload(client, owner, kb, "报价.csv", "供应商,单价\n甲,3800\n".encode())
            if scenario == "unlimited":
                async with sessions.begin() as db:
                    job = await db.get(KnowledgeJob, uploaded["job_id"])
                    job.processing_reserved_tokens = 100001
                    job.reserved_tokens = 200001
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                index = await db.get(KnowledgeIndex, job.index_id)
                saved = await db.get(KnowledgeBase, kb["id"])
                assert saved.processing_token_budget is None and saved.embedding_token_budget is None
                assert index.configuration["processing_token_budget"] is None and index.configuration["embedding_token_budget"] is None
                if scenario == "unlimited":
                    assert job.state == KnowledgeJobState.COMPLETED
                    assert job.processing_tokens == 120020 and job.embedding_tokens == 300000
                    assert job.processing_reserved_tokens > 100001 and job.reserved_tokens > 200001
                    assert job.processing_call_count == 1 and job.processing_usage
                    assert saved.active_index_id == index.id
                else:
                    assert job.state == KnowledgeJobState.FAILED and job.failure_code == KnowledgeFailure.BUDGET_EXCEEDED
                    assert saved.active_index_id != index.id
                assert model.calls == (0 if scenario == "processing-reserved-cost" else 1)
                assert bool(embedded) == (scenario in {"unlimited", "processing-actual-cost", "embedding-actual-cost"})

    asyncio.run(exercise())
