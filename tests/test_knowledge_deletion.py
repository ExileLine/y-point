# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 14:47:45
# @Author  : yangyuexiong
# @File    : test_knowledge_deletion.py

"""通过正式 HTTP 与索引任务验证模型不可用时的知识文档删除。"""

import asyncio
import io

import pytest
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import func, select

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import ArtifactState, KnowledgeIndexState, KnowledgeJobState, KnowledgeProcessingMode, KnowledgeSearchMode
from app.models.artifact import Artifact
from app.models.knowledge import KnowledgeBase, KnowledgeDocument, KnowledgeDocumentVersion, KnowledgeIndex, KnowledgeJob
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services import knowledge as knowledge_service, knowledge_jobs
from app.services.embedding_adapter import EmbeddingResult
from app.services.knowledge_jobs import process_next_knowledge_job, prune_deleted_knowledge_blobs
from tests.test_knowledge import _create_kb, _drain, _search, _seed_models, _upload
from tests.test_m51_governance import _database, _http_platform


def test_document_deletion_ignores_disabled_processing_model(tmp_path, monkeypatch) -> None:
    """已配置加工模型停用后仍能删除文档，并立即阻断其检索与固定引用。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """在隔离数据库建立真实索引后停用模型，复现删除事务被回滚的问题。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _seed_models(sessions)
            kb = await _create_kb(client, owner, processing_mode=KnowledgeProcessingMode.ORIGINAL, processing_model_id=1)
            uploaded = await _upload(client, owner, kb, "待删除.txt", "报销期限为十五个工作日。".encode())
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "报销期限"))["hits"][0]
            async with sessions.begin() as db:
                model = await db.get(ModelCatalog, 1)
                model.status = 0
            document_id = uploaded["document"]["id"]
            removed = await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}?expected_revision={kb['revision']}", headers=owner)
            assert removed.status_code == 200, removed.text
            assert (await _search(client, owner, kb["id"], "报销期限"))["no_answer"]
            assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}", headers=owner)).status_code == 404

    asyncio.run(exercise())


async def _fixed_embeddings(model, provider, texts, *, dimensions) -> EmbeddingResult:
    """用确定性单位向量验证索引事务，不请求真实供应商。"""
    assert dimensions == 2
    return EmbeddingResult([[1.0, 0.0] for _ in texts], len(texts), None)


@pytest.mark.parametrize("model_id", [1, 2], ids=["processing", "embedding"])
@pytest.mark.parametrize("invalidity", ["disabled", "deleted", "missing", "unverified", "provider_disabled", "provider_deleted", "provider_missing"])
def test_document_deletion_with_unavailable_models(tmp_path, monkeypatch, model_id: int, invalidity: str) -> None:
    """模型与供应商失效均不阻断删除，其他文档和授权引用继续保留。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(knowledge_jobs, "embed_texts", _fixed_embeddings)

    async def exercise() -> None:
        """覆盖混合索引、旧版本引用、共享访问、旧任务取消和后台物理清理。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, reader = headers
            await _seed_models(sessions)
            kb = await _create_kb(
                client,
                owner,
                search_mode=KnowledgeSearchMode.HYBRID,
                embedding_model_id=2,
                processing_mode=KnowledgeProcessingMode.ORIGINAL,
                processing_model_id=1,
            )
            removed = await _upload(client, owner, kb, "待删除.txt", "报销期限为十五个工作日。".encode())
            await _drain(sessions)
            first_hit = (await _search(client, owner, kb["id"], "报销期限", keyword_only=True))["hits"][0]
            await _upload(client, owner, kb, "待删除.txt", "报销期限为三十个工作日。".encode(), document_id=removed["document"]["id"])
            await _upload(client, owner, kb, "保留.txt", "技术支持的联系部门为平台组。".encode())
            await _drain(sessions)
            latest_hit = (await _search(client, owner, kb["id"], "报销期限", keyword_only=True))["hits"][0]
            kept_hit = (await _search(client, owner, kb["id"], "技术支持", keyword_only=True))["hits"][0]
            shared = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/shares",
                headers=owner,
                json={
                    "user_id": 2,
                },
            )
            assert shared.status_code == 200
            pending = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            assert pending.status_code == 202, pending.text
            kb["revision"] = pending.json()["data"]["revision"]
            pending_id = pending.json()["data"]["job_id"]
            async with sessions.begin() as db:
                model = await db.get(ModelCatalog, model_id)
                provider = await db.get(ProviderConnection, model.provider_id)
                if invalidity == "disabled":
                    model.status = 0
                elif invalidity == "deleted":
                    model.is_deleted = 1
                elif invalidity == "missing":
                    await db.delete(model)
                elif invalidity == "unverified":
                    model.verified_capabilities = []
                elif invalidity == "provider_disabled":
                    provider.status = 0
                elif invalidity == "provider_deleted":
                    provider.is_deleted = 1
                else:
                    await db.delete(provider)
                jobs_before = await db.scalar(select(func.count()).select_from(KnowledgeJob))
            document_id = removed["document"]["id"]
            result = await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}?expected_revision={kb['revision']}", headers=owner)
            assert result.status_code == 200, result.text
            deletion = result.json()["data"]
            assert deletion["revision"] == kb["revision"] + 1
            assert deletion["job_id"] is None and deletion["rebuild_required"] is True and deletion["rebuild_reason"]
            docs = (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents", headers=owner)).json()["data"]
            assert [document["filename"] for document in docs] == ["保留.txt"]
            for actor in (owner, reader):
                assert (await _search(client, actor, kb["id"], "报销期限", keyword_only=True))["no_answer"]
                assert (await _search(client, actor, kb["id"], "技术支持", keyword_only=True))["hits"][0]["chunk_id"] == kept_hit["chunk_id"]
                for hit in (first_hit, latest_hit):
                    assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}", headers=actor)).status_code == 404
                assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{kept_hit['chunk_id']}", headers=actor)).status_code == 200
                assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}/processing-preview", headers=actor)).status_code == 404
            async with sessions() as db:
                assert (await db.get(KnowledgeDocument, document_id)).is_deleted == 1
                assert (await db.get(KnowledgeBase, kb["id"])).active_index_id == kept_hit["index_id"]
                assert await db.scalar(select(func.count()).select_from(KnowledgeJob)) == jobs_before
                assert (await db.get(KnowledgeJob, pending_id)).state == KnowledgeJobState.CANCEL_REQUESTED
            await _drain(sessions)
            async with sessions() as db:
                assert (await db.get(KnowledgeJob, pending_id)).state == KnowledgeJobState.CANCELLED
                assert (await db.get(KnowledgeBase, kb["id"])).active_index_id == kept_hit["index_id"]
            assert await prune_deleted_knowledge_blobs(sessions) == 2
            async with sessions() as db:
                versions = (await db.scalars(select(KnowledgeDocumentVersion).where(KnowledgeDocumentVersion.document_id == document_id))).all()
                for version in versions:
                    artifact = await db.get(Artifact, version.artifact_id)
                    assert artifact.state == ArtifactState.DELETED and artifact.is_deleted == 1

    asyncio.run(exercise())


def test_delete_last_pdf_with_unavailable_embedding_then_recover(tmp_path, monkeypatch) -> None:
    """最后一份 PDF 删除后不再调用失效向量模型，旧原页被阻断，修复模型可重建。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(knowledge_jobs, "embed_texts", _fixed_embeddings)

    async def unexpected_embedding(*args, **kwargs):
        """删除后的空知识库检索不应发送查询嵌入请求。"""
        raise AssertionError("空知识库不应调用向量模型")

    monkeypatch.setattr(knowledge_service, "embed_texts", unexpected_embedding)

    async def exercise() -> None:
        """检查真实 PDF 的授权原页在删除前可读，删除后对所有版本统一拒绝。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _seed_models(sessions)
            kb = await _create_kb(client, owner, search_mode=KnowledgeSearchMode.HYBRID, embedding_model_id=2)
            stream = io.BytesIO()
            canvas = Canvas(stream)
            canvas.drawString(48, 700, "Travel allowance is 5000 CNY.")
            canvas.save()
            upload = await _upload(client, owner, kb, "allowance.pdf", stream.getvalue())
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "allowance", keyword_only=True))["hits"][0]
            page_url = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}/page-image"
            original_page = await client.get(page_url, headers=owner)
            assert original_page.status_code == 200 and original_page.content.startswith(b"\x89PNG")
            async with sessions.begin() as db:
                (await db.get(ModelCatalog, 2)).status = 0
            document_id = upload["document"]["id"]
            result = await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}?expected_revision={kb['revision']}", headers=owner)
            assert result.status_code == 200, result.text
            assert (await client.get(page_url, headers=owner)).status_code == 404
            assert (await _search(client, owner, kb["id"], "allowance"))["no_answer"]
            async with sessions.begin() as db:
                assert (await db.get(KnowledgeBase, kb["id"])).active_index_id is None
                (await db.get(ModelCatalog, 2)).status = 1
            rebuilt = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": result.json()["data"]["revision"],
                },
            )
            assert rebuilt.status_code == 202, rebuilt.text
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, rebuilt.json()["data"]["job_id"])
                index = await db.get(KnowledgeIndex, job.index_id)
                assert job.state == KnowledgeJobState.COMPLETED and index.state == KnowledgeIndexState.READY
                assert index.document_versions == [] and index.chunk_count == 0
            assert (await client.get(page_url, headers=owner)).status_code == 404

    asyncio.run(exercise())


def test_delete_checks_revision_permission_and_unexpected_errors(tmp_path, monkeypatch) -> None:
    """版本冲突、越权与非模型异常仍回滚删除及取消标记，不误报删除成功。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """故障注入只在索引配置准备阶段生效，不放宽正式上传或重建的校验。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, stranger = headers
            await _seed_models(sessions)
            kb = await _create_kb(client, owner, processing_mode=KnowledgeProcessingMode.ORIGINAL, processing_model_id=1)
            uploaded = await _upload(client, owner, kb, "保留.txt", "权限校验必须保留的原文。".encode())
            document_id = uploaded["document"]["id"]
            endpoint = f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}"
            async with sessions.begin() as db:
                (await db.get(ModelCatalog, 1)).status = 0
            assert (await client.delete(f"{endpoint}?expected_revision={kb['revision']}", headers=stranger)).status_code == 404
            assert (await client.delete(f"{endpoint}?expected_revision={kb['revision'] - 1}", headers=owner)).status_code == 409
            async with sessions() as db:
                assert (await db.get(KnowledgeDocument, document_id)).is_deleted == 0
                assert (await db.get(KnowledgeJob, uploaded["job_id"])).state == KnowledgeJobState.UPLOADED
                assert (await db.get(KnowledgeBase, kb["id"])).revision == kb["revision"]
            rebuilding = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            assert rebuilding.status_code == 409

            async def unavailable_configuration(*args, **kwargs):
                """模拟数据库以外的临时服务异常，不应当作模型校验失败吞掉。"""
                raise CustomException(status_code=503, custom_code=10005, detail="配置读取暂不可用")

            with monkeypatch.context() as patch:
                patch.setattr(knowledge_service, "validate_knowledge_configuration", unavailable_configuration)
                failed = await client.delete(f"{endpoint}?expected_revision={kb['revision']}", headers=owner)
                assert failed.status_code == 503
            async with sessions() as db:
                assert (await db.get(KnowledgeDocument, document_id)).is_deleted == 0
                assert (await db.get(KnowledgeJob, uploaded["job_id"])).state == KnowledgeJobState.UPLOADED
            async with sessions.begin() as db:
                (await db.get(ModelCatalog, 1)).status = 1
            success = await client.delete(f"{endpoint}?expected_revision={kb['revision']}", headers=owner)
            assert success.status_code == 200
            result = success.json()["data"]
            assert result["job_id"] is not None and result["rebuild_required"] is False and result["rebuild_reason"] is None
            await _drain(sessions)
            async with sessions() as db:
                assert (await db.get(KnowledgeJob, uploaded["job_id"])).state == KnowledgeJobState.CANCELLED
                assert (await db.get(KnowledgeJob, result["job_id"])).state == KnowledgeJobState.COMPLETED

    asyncio.run(exercise())


def test_running_index_cannot_publish_after_deletion(tmp_path, monkeypatch) -> None:
    """模型等待中的旧索引在删除后被取消，迟到结果不能恢复已删文档。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    monkeypatch.setattr(knowledge_jobs, "embed_texts", _fixed_embeddings)

    async def exercise() -> None:
        """让实际 worker 停在嵌入阶段，提交删除后再释放响应并核对发布指针。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            await _seed_models(sessions)
            kb = await _create_kb(client, owner, search_mode=KnowledgeSearchMode.HYBRID, embedding_model_id=2)
            upload = await _upload(client, owner, kb, "旧任务.txt", "旧任务中的差旅制度。".encode())
            await _drain(sessions)
            old_hit = (await _search(client, owner, kb["id"], "差旅制度", keyword_only=True))["hits"][0]
            pending = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            assert pending.status_code == 202
            started, release = asyncio.Event(), asyncio.Event()

            async def delayed_embedding(*args, **kwargs):
                """模拟已发送到远端且延迟返回的嵌入请求。"""
                started.set()
                await release.wait()
                return await _fixed_embeddings(*args, **kwargs)

            monkeypatch.setattr(knowledge_jobs, "embed_texts", delayed_embedding)
            worker = asyncio.create_task(process_next_knowledge_job(sessions))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
                async with sessions.begin() as db:
                    (await db.get(ModelCatalog, 2)).status = 0
                document_id = upload["document"]["id"]
                revision = pending.json()["data"]["revision"]
                deleted = await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}?expected_revision={revision}", headers=owner)
                assert deleted.status_code == 200, deleted.text
            finally:
                release.set()
                await asyncio.wait_for(worker, timeout=5)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, pending.json()["data"]["job_id"])
                assert job.state == KnowledgeJobState.CANCELLED
                assert (await db.get(KnowledgeBase, kb["id"])).active_index_id is None
                assert (await db.get(KnowledgeIndex, job.index_id)).state != KnowledgeIndexState.READY
            assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{old_hit['chunk_id']}", headers=owner)).status_code == 404

    asyncio.run(exercise())
