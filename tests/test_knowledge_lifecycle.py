# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 12:14:59
# @Author  : yangyuexiong
# @File    : test_knowledge_lifecycle.py

"""知识文档结构、后台任务取消及独立 worker 故障恢复验收。"""

import asyncio
import io
import os
import sys
from datetime import timedelta

from docx import Document
from pypdf import PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import func, select, update

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, KnowledgeIndexState, KnowledgeJobState
from app.models.base import shanghai_now
from app.models.knowledge import KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeIndex, KnowledgeJob
from app.models.tool_execution import ToolExecutionLease
from app.services import knowledge_jobs
from app.services.embedding_adapter import EmbeddingResult
from app.services.knowledge_jobs import process_next_knowledge_job
from tests.test_knowledge import _create_kb, _drain, _search, _seed_models, _upload
from tests.test_m51_governance import _database, _http_platform


def test_pdf_docx_indexing_and_scanned_failure(tmp_path, monkeypatch):
    """真实解析中文 PDF 页码与 DOCX 标题表格，扫描件明确失败且保留旧代。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """通过上传接口检查规范原文、位置和历史索引完整性。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner)
            output = io.BytesIO()
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
            canvas = Canvas(output)
            canvas.setFont("STSong-Light", 14)
            canvas.drawString(40, 700, "报销申请需要审批")
            canvas.showPage()
            canvas.setFont("STSong-Light", 14)
            canvas.drawString(40, 700, "住宿费用上限为五千元")
            canvas.save()
            await _upload(client, owner, kb, "差旅.pdf", output.getvalue())
            document = Document()
            document.add_heading("费用报销", 1)
            table = document.add_table(rows=1, cols=2)
            table.cell(0, 0).text, table.cell(0, 1).text = "打车额度", "一百元"
            output = io.BytesIO()
            document.save(output)
            await _upload(client, owner, kb, "表格.docx", output.getvalue())
            await _drain(sessions)
            pdf_hit = (await _search(client, owner, kb["id"], "住宿费用"))["hits"][0]
            assert pdf_hit["filename"] == "差旅.pdf" and pdf_hit["page"] == 2
            table_hit = (await _search(client, owner, kb["id"], "打车额度"))["hits"][0]
            assert table_hit["page"] is None and table_hit["heading"] == "费用报销" and "一百元" in table_hit["content"]
            async with sessions() as db:
                for hit in (pdf_hit, table_hit):
                    version = await db.scalar(
                        select(KnowledgeDocumentVersion).where(
                            KnowledgeDocumentVersion.document_id == hit["document_id"],
                            KnowledgeDocumentVersion.version == hit["document_version"],
                        ),
                    )
                    assert hit["content"] == version.extracted_text[hit["start_offset"] : hit["end_offset"]]
            output, writer = io.BytesIO(), PdfWriter()
            writer.add_blank_page(width=300, height=300)
            writer.write(output)
            scanned = await _upload(client, owner, kb, "扫描件.pdf", output.getvalue())
            await _drain(sessions)
            state = (await client.get(f"/api/v1/knowledge-bases/jobs/{scanned['job_id']}", headers=owner)).json()["data"]
            assert state["failure_code"] == KnowledgeFailure.OCR_REQUIRED
            assert (await _search(client, owner, kb["id"], "住宿费用"))["hits"][0]["index_id"] == pdf_hit["index_id"]

    asyncio.run(exercise())


def test_embedding_cancel_timeout_and_retry_budget(tmp_path, monkeypatch):
    """外部嵌入等待期间可取消或总超时，重试保留预留预算和用量。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """在模型等待时并发修改任务状态并核对无半成品及资源泄漏。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _seed_models(sessions)
            started = asyncio.Event()

            async def never_finishes(*args, **kwargs):
                """模拟已发送到远端但尚未收到结果的请求。"""
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(knowledge_jobs, "embed_texts", never_finishes)
            kb = await _create_kb(client, owner, search_mode="hybrid", embedding_model_id=2, embedding_token_budget=50)
            uploaded = await _upload(client, owner, kb, "取消.txt", "报销期限为十五天。".encode())
            task = asyncio.create_task(process_next_knowledge_job(sessions))
            await asyncio.wait_for(started.wait(), 10)
            cancelled = await client.post(f"/api/v1/knowledge-bases/jobs/{uploaded['job_id']}/cancel", headers=owner)
            assert cancelled.status_code == 200
            await asyncio.wait_for(task, 5)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.CANCELLED and job.reserved_tokens > 0
                assert (await db.get(KnowledgeIndex, job.index_id)).state == KnowledgeIndexState.CANCELLED
                assert await db.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0
            retry = await client.post(f"/api/v1/knowledge-bases/jobs/{uploaded['job_id']}/retry?expected_attempts=1", headers=owner)
            assert retry.status_code == 200
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.failure_code == KnowledgeFailure.BUDGET_EXCEEDED
                assert job.attempts == 2
            monkeypatch.setattr(knowledge_jobs, "KNOWLEDGE_JOB_TIMEOUT", 0.15)
            kb = await _create_kb(client, owner, search_mode="hybrid", embedding_model_id=2)
            uploaded = await _upload(client, owner, kb, "超时.txt", "超时验证".encode())
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.failure_code == KnowledgeFailure.TIMEOUT
                assert job.lease_token is None
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0

    asyncio.run(exercise())


def test_worker_process_restart_and_concurrent_claim(tmp_path, monkeypatch):
    """SIGKILL 独立 worker 后由正式入口恢复任务，再双进程争抢且只发布一次。"""
    files = tmp_path / "files"
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(files))

    async def exercise():
        """使用隔离业务库和检查点，进程退出不影响当前开发服务。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner)
            uploaded = await _upload(client, owner, kb, "恢复.txt", "恢复报销规则".encode())
            marker = tmp_path / "parsing-started"
            env = {
                **os.environ,
                "DB_BACKEND": "sqlite",
                "SQLITE_DATABASE": str(tmp_path / "governance.db"),
                "CHECKPOINT_BACKEND": "sqlite",
                "ARTIFACT_STORAGE_DIR": str(files),
                "WORKER_LOG_FILE": str(tmp_path / "worker.log"),
            }
            checkpoint = tmp_path / "worker-checkpoints.db"
            script = "\n".join(
                [
                    "import asyncio",
                    "from pathlib import Path",
                    "from app.services import knowledge_jobs",
                    "from app.cli.run_worker import run_worker",
                    "async def waiting(*args):",
                    '    """模拟进程在解析阶段中断。"""',
                    f"    Path({str(marker)!r}).touch()",
                    "    await asyncio.Event().wait()",
                    "knowledge_jobs.parse_knowledge_isolated = waiting",
                    f"asyncio.run(run_worker(Path({str(checkpoint)!r}), once=True))",
                ],
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(15):
                    while not marker.exists():
                        assert process.returncode is None
                        await asyncio.sleep(0.05)
            finally:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            async with sessions.begin() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.PARSING and job.attempts == 1
                job.lease_expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
                await db.execute(update(ToolExecutionLease).values(expires_at=shanghai_now(naive=True) - timedelta(seconds=1)))

            async def official_worker():
                """执行一次正式 CLI 并验证进程成功退出。"""
                worker = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.cli.run_worker",
                    "--once",
                    "--checkpoint",
                    str(checkpoint),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await asyncio.wait_for(worker.communicate(), 45)
                assert worker.returncode == 0, (output + error).decode()

            await official_worker()
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED and job.attempts == 2
                assert any(step.get("skipped") and step["stage"] == KnowledgeJobState.EMBEDDING for step in job.progress)
            uploaded = await _upload(client, owner, kb, "争抢.txt", "争抢报销规则".encode())
            await asyncio.gather(official_worker(), official_worker())
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED and job.attempts == 1
                index = await db.get(KnowledgeIndex, job.index_id)
                assert index.chunk_count == await db.scalar(select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.index_id == index.id))
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0

    asyncio.run(exercise())


def test_revision_upload_limit_and_embedding_probe_permissions(tmp_path, monkeypatch):
    """并发旧版本拒绝、无长度上传有界、嵌入探测仅管理员且失败撤销验证。"""
    from app.api.v1 import knowledge as knowledge_api
    from app.core.enums import ModelCapability, UserRole
    from app.models.admin import Admin
    from app.models.artifact import Artifact
    from app.models.model_catalog import ModelCatalog
    from app.services.knowledge_parser import KnowledgeProcessingError

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """从 HTTP 验证边界错误不留下新文件、索引任务或错误能力标记。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            await _seed_models(sessions)
            kb = await _create_kb(client, owner)
            await _upload(client, owner, kb, "制度.txt", "有效制度".encode())
            stale = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                headers=owner,
                data={
                    "expected_revision": "1",
                },
                files={
                    "file": ("过期.txt", b"stale", "text/plain"),
                },
            )
            assert stale.status_code == 409

            async def stream_upload():
                """不提供 Content-Length，实际接收超过十一 MiB 后必须中断。"""
                yield (
                    b'--knowledge\r\nContent-Disposition: form-data; name="expected_revision"\r\n\r\n2\r\n'
                    b'--knowledge\r\nContent-Disposition: form-data; name="file"; filename="huge.txt"\r\n'
                    b"Content-Type: text/plain\r\n\r\n"
                )
                for _ in range(12):
                    yield b"x" * (1024 * 1024)
                yield b"\r\n--knowledge--\r\n"

            response = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                headers={
                    **owner,
                    "content-type": "multipart/form-data; boundary=knowledge",
                },
                content=stream_upload(),
            )
            assert response.status_code == 413
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(Artifact)) == 1
                assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == 1
                version = await db.scalar(select(KnowledgeDocumentVersion))
                artifact_id = version.artifact_id
            assert (await client.delete(f"/api/v1/artifacts/{artifact_id}", headers=owner)).status_code == 409
            assert (await client.post("/api/v1/models/2/embedding-test", headers=other)).status_code == 403
            async with sessions.begin() as db:
                await db.execute(update(Admin).where(Admin.id == 1).values(role=UserRole.ADMIN))

            async def probe(*args, **kwargs):
                """模拟独立探测读取到实际三维向量。"""
                return EmbeddingResult([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], 12, None)

            generic_probe = await client.post(
                "/api/v1/models/2/capabilities/test",
                headers=owner,
                json={
                    "capability": ModelCapability.EMBEDDING,
                },
            )
            assert generic_probe.status_code == 400
            monkeypatch.setattr(knowledge_api, "embed_texts", probe)
            response = await client.post("/api/v1/models/2/embedding-test", headers=owner)
            assert response.json()["data"]["dimensions"] == 3

            async def unavailable(*args, **kwargs):
                """上游失败后不能继续沿用旧的嵌入验证。"""
                raise KnowledgeProcessingError(KnowledgeFailure.AUTHENTICATION)

            monkeypatch.setattr(knowledge_api, "embed_texts", unavailable)
            response = await client.post("/api/v1/models/2/embedding-test", headers=owner)
            assert not response.json()["data"]["success"]
            async with sessions() as db:
                model = await db.get(ModelCatalog, 2)
                assert model.embedding_dimensions is None and ModelCapability.EMBEDDING not in model.verified_capabilities

    asyncio.run(exercise())


def test_graceful_worker_stop_requeues_with_retry_limit(tmp_path, monkeypatch):
    """正常停止 worker 自动重新排队，保留费用预留并在第三次中断后终止重试。"""
    import pytest

    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """区分 worker 退出与用户取消，确保重启不吞掉后台任务。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _seed_models(sessions)
            started = asyncio.Event()

            async def waiting(*args, **kwargs):
                """等待模拟远端响应，以便在模型调用中停止 worker。"""
                started.set()
                await asyncio.Event().wait()

            monkeypatch.setattr(knowledge_jobs, "embed_texts", waiting)
            kb = await _create_kb(client, owner, search_mode="hybrid", embedding_model_id=2)
            uploaded = await _upload(client, owner, kb, "中断.txt", "中断后的知识任务可恢复".encode())
            for attempt in range(1, 4):
                started.clear()
                worker = asyncio.create_task(process_next_knowledge_job(sessions))
                await asyncio.wait_for(started.wait(), 10)
                worker.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await worker
                async with sessions() as db:
                    job = await db.get(KnowledgeJob, uploaded["job_id"])
                    assert job.attempts == attempt and job.reserved_tokens > 0
                    assert job.failure_code == KnowledgeFailure.WORKER_INTERRUPTED and job.lease_token is None
                    assert job.state == (KnowledgeJobState.UPLOADED if attempt < 3 else KnowledgeJobState.FAILED)
                    assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0
            assert not await process_next_knowledge_job(sessions)
            assert (await client.post(f"/api/v1/knowledge-bases/jobs/{uploaded['job_id']}/retry?expected_attempts=3", headers=owner)).status_code == 409

    asyncio.run(exercise())
