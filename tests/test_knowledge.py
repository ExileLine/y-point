# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 12:00:48
# @Author  : yangyuexiong
# @File    : test_knowledge.py

"""通过真实 HTTP、原生全文检索和持久化 worker 验证知识库行为。"""

import asyncio
import io
import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from docx import Document
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from pypdf import PdfWriter
from sqlalchemy import func, select

from app.core.config import get_config
from app.core.enums import KnowledgeFailure, KnowledgeIndexState, KnowledgeJobState, ModelCapability, ProviderKind, UserRole
from app.models.admin import Admin
from app.models.artifact import Artifact
from app.models.base import shanghai_now
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeDocumentVersion,
    KnowledgeIndex,
    KnowledgeJob,
    KnowledgeRetrieval,
    RunKnowledgeBase,
)
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.tool_execution import ToolExecutionLease
from app.schemas.knowledge import KnowledgeSearchIn
from app.services import knowledge as knowledge_service, knowledge_jobs
from app.services.embedding_adapter import EmbeddingResult, embed_texts, normalize_vectors
from app.services.knowledge import read_knowledge_citation, search_knowledge
from app.services.knowledge_jobs import process_next_knowledge_job, prune_deleted_knowledge_blobs
from app.services.knowledge_parser import KnowledgeProcessingError, chunk_knowledge, parse_knowledge, parse_knowledge_isolated
from app.services.run_executor import execute_text_run
from app.services.run_manager import claim_run
from app.services.worker_tools import make_worker_tool_resolver
from tests.test_m51_governance import _database, _http_platform

EVALUATION = json.loads((Path(__file__).parent / "fixtures" / "m6_retrieval.json").read_text())


async def _create_kb(client, headers, **configuration):
    """使用正式接口创建私有知识库并检查统一响应。"""
    response = await client.post(
        "/api/v1/knowledge-bases",
        headers=headers,
        json={
            "name": "测试知识库",
            **configuration,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def _upload(client, headers, kb, filename, content, *, document_id=None):
    """通过 multipart 上传到指定知识库，更新时使用显式文档 ID。"""
    endpoint = f"/api/v1/knowledge-bases/{kb['id']}/documents"
    method = client.post
    if document_id is not None:
        endpoint += f"/{document_id}"
        method = client.put
    response = await method(
        endpoint,
        headers=headers,
        data={
            "expected_revision": str(kb["revision"]),
        },
        files={
            "file": (filename, content),
        },
    )
    assert response.status_code == 202, response.text
    data = response.json()["data"]
    kb["revision"] = data["revision"]
    return data


async def _drain(sessions):
    """使用正式任务执行函数处理本测试的有界队列。"""
    for _ in range(30):
        if not await process_next_knowledge_job(sessions):
            return
    raise AssertionError("知识任务未在预算内排空")


async def _search(client, headers, kb_id, query, **options):
    """执行正式检索接口，返回包含固定引用的结果。"""
    response = await client.post(
        f"/api/v1/knowledge-bases/{kb_id}/search",
        headers=headers,
        json={
            "query": query,
            **options,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def _seed_models(sessions):
    """向隔离库放入已验证的聊天模型和测试用独立嵌入模型。"""
    async with sessions.begin() as db:
        owner = await db.get(Admin, 1)
        owner.role = UserRole.ADMIN
        db.add(
            ProviderConnection(
                id=1, name="m6-local", name_key="m6-local", kind=ProviderKind.OLLAMA, base_url="http://127.0.0.1:11434/v1", creator_id=1
            )
        )
        db.add(
            ModelCatalog(
                id=1,
                provider_id=1,
                external_id="gemma4:31b-cloud",
                display_name="M6 chat",
                capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                verified_capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                capabilities_verified=True,
                creator_id=1,
            )
        )
        db.add(
            ModelCatalog(
                id=2,
                provider_id=1,
                external_id="embeddinggemma",
                display_name="M6 embedding",
                capabilities=[ModelCapability.EMBEDDING],
                verified_capabilities=[ModelCapability.EMBEDDING],
                embedding_dimensions=2,
                capabilities_verified=True,
                creator_id=1,
            )
        )


def test_parser_formats_exact_offsets_and_scanned_pdf() -> None:
    """TXT、Markdown、DOCX 结构、超长重叠及扫描 PDF 的失败类别保持明确。"""
    text = "# 制度\r\n差旅报销" + "规定" * 700 + "\n## 审批\n负责人审批。"
    parsed = parse_knowledge(text.encode(), "text/markdown")
    chunks = chunk_knowledge(parsed, 300, 50)
    assert len(chunks) > 3
    for chunk in chunks:
        assert chunk["content"] == parsed.text[chunk["start_offset"] : chunk["end_offset"]]
        assert len(chunk["content"]) <= 300
    assert chunks[-1]["heading"] == "审批"
    document = Document()
    document.add_heading("报销流程", 1)
    document.add_paragraph("先填写申请")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "金额", "五千元"
    output = io.BytesIO()
    document.save(output)
    parsed = parse_knowledge(output.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "金额 | 五千元" in parsed.text and parsed.blocks[-1]["heading"] == "报销流程"
    assert all(block["page"] is None for block in parsed.blocks)
    writer, output = PdfWriter(), io.BytesIO()
    writer.add_blank_page(width=300, height=300)
    writer.write(output)
    with pytest.raises(KnowledgeProcessingError) as failure:
        parse_knowledge(output.getvalue(), "application/pdf")
    assert failure.value.code == KnowledgeFailure.OCR_REQUIRED
    parsed = asyncio.run(parse_knowledge_isolated("中文子进程解析".encode(), "text/plain"))
    assert parsed.text == "中文子进程解析"


def test_fixed_chinese_evaluation_permissions_citations_and_budgets(tmp_path, monkeypatch) -> None:
    """先验固定中文集达到 Recall@3、引用精确和零越权/无答案误命中阈值。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """使用真实上传和 worker 构建 SQLite FTS5，并验证完整管理接口。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            alice, bob = headers
            kb, secret = await _create_kb(client, alice), await _create_kb(client, bob)
            documents = {}
            for sample in EVALUATION["documents"]:
                target, actor = (kb, alice) if sample["owner"] == "alice" else (secret, bob)
                documents[sample["name"]] = await _upload(client, actor, target, sample["name"], sample["content"].encode())
            assert (await _search(client, alice, kb["id"], "差旅报销"))["no_answer"]
            await _drain(sessions)
            correct = 0
            for question in EVALUATION["questions"]:
                result = await _search(client, alice, kb["id"], question["query"], top_k=3)
                assert any(
                    hit["filename"] == question["expected_document"] and question["expected_text"] in hit["content"]
                    for hit in result["hits"]
                ), question
                correct += 1
                async with sessions() as db:
                    for hit in result["hits"]:
                        chunk = await db.get(KnowledgeChunk, hit["chunk_id"])
                        version = await db.get(KnowledgeDocumentVersion, chunk.document_version_id)
                        assert hit["content"] == version.extracted_text[hit["start_offset"] : hit["end_offset"]]
                        assert (await read_knowledge_citation(db, 1, kb["id"], hit["chunk_id"]))["content"] == hit["content"]
            assert correct / len(EVALUATION["questions"]) >= EVALUATION["thresholds"]["keyword_recall_at_3"]
            for query in EVALUATION["unanswerable"]:
                assert (await _search(client, alice, kb["id"], query))["no_answer"]
            assert (await _search(client, alice, kb["id"], EVALUATION["permission_query"]))["no_answer"]
            assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=bob)).status_code == 404
            shared = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/shares",
                headers=alice,
                json={
                    "user_id": 2,
                },
            )
            assert shared.status_code == 200
            result = await _search(client, bob, kb["id"], "差旅报销", top_k=1, max_bytes=512)
            assert len(result["hits"]) == 1 and result["byte_count"] <= 512
            citation = result["hits"][0]["chunk_id"]
            assert (
                await client.post(
                    f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                    headers=bob,
                    json={
                        "expected_revision": kb["revision"],
                    },
                )
            ).status_code == 404
            assert (await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/shares/2", headers=alice)).status_code == 200
            assert (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{citation}", headers=bob)).status_code == 404
            assert (
                await client.post(
                    f"/api/v1/knowledge-bases/{kb['id']}/search",
                    headers=bob,
                    json={
                        "query": "报销",
                    },
                )
            ).status_code == 404

    asyncio.run(exercise())


def test_index_update_dedup_failure_retry_delete_and_history(tmp_path, monkeypatch) -> None:
    """更新原子切代、哈希去重、失败保留旧代、旧引用与删除即时失效。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """同时检查文件计数、租约释放及删除后物理清理。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner)
            first = await _upload(client, owner, kb, "规则.txt", "报销期限为十五天。".encode())
            await _drain(sessions)
            old_hit = (await _search(client, owner, kb["id"], "报销期限"))["hits"][0]
            duplicate = await _upload(client, owner, kb, "规则.txt", "报销期限为十五天。".encode())
            assert duplicate["deduplicated"] and duplicate["job_id"] is None
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(Artifact)) == 1
            document_id = first["document"]["id"]
            changed = await _upload(client, owner, kb, "规则.txt", "报销期限为三十天。".encode(), document_id=document_id)
            assert "十五天" in (await _search(client, owner, kb["id"], "报销期限"))["hits"][0]["content"]
            await _drain(sessions)
            new_hit = (await _search(client, owner, kb["id"], "报销期限"))["hits"][0]
            assert new_hit["document_version"] == 2 and "三十天" in new_hit["content"]
            async with sessions() as db:
                old = await read_knowledge_citation(db, 1, kb["id"], old_hit["chunk_id"])
                assert "十五天" in old["content"]
            broken = await _upload(client, owner, kb, "规则.txt", "报销期限为四十天。".encode(), document_id=document_id)

            async def unavailable_parser(*args):
                """模拟合法上传后解析子进程失败，旧索引应持续提供服务。"""
                raise KnowledgeProcessingError(KnowledgeFailure.PARSE_FAILED)

            with monkeypatch.context() as parser_patch:
                parser_patch.setattr(knowledge_jobs, "parse_knowledge_isolated", unavailable_parser)
                await _drain(sessions)
            state = (await client.get(f"/api/v1/knowledge-bases/jobs/{broken['job_id']}", headers=owner)).json()["data"]
            assert state["state"] == KnowledgeJobState.FAILED and state["failure_code"] == KnowledgeFailure.PARSE_FAILED
            assert "lease_token" not in state
            assert (await _search(client, owner, kb["id"], "报销期限"))["hits"][0]["index_id"] == new_hit["index_id"]
            retry = await client.post(f"/api/v1/knowledge-bases/jobs/{broken['job_id']}/retry?expected_attempts=1", headers=owner)
            assert retry.status_code == 200
            await _drain(sessions)
            assert (await client.get(f"/api/v1/knowledge-bases/jobs/{broken['job_id']}", headers=owner)).json()["data"]["attempts"] == 2
            removed = await client.delete(
                f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}?expected_revision={kb['revision']}", headers=owner
            )
            assert removed.status_code == 200, removed.text
            assert (await _search(client, owner, kb["id"], "报销期限"))["no_answer"]
            assert (
                await client.get(f"/api/v1/knowledge-bases/{kb['id']}/citations/{old_hit['chunk_id']}", headers=owner)
            ).status_code == 404
            await _drain(sessions)
            assert await prune_deleted_knowledge_blobs(sessions) == 3
            async with sessions() as db:
                assert await db.scalar(select(func.count()).select_from(ToolExecutionLease)) == 0
                assert (await db.get(KnowledgeIndex, old_hit["index_id"])).state == KnowledgeIndexState.READY
                assert (await db.get(KnowledgeJob, changed["job_id"])).state == KnowledgeJobState.COMPLETED

    asyncio.run(exercise())


def test_cancel_expired_lease_and_superseded_job(tmp_path, monkeypatch) -> None:
    """排队取消、过期租约重试、旧代超越及最大尝试次数有界且不覆盖旧索引。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """持久化模拟进程失联并经正式领取入口恢复。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner)
            first = await _upload(client, owner, kb, "规则.txt", "知识租约恢复验证。".encode())
            assert (await client.post(f"/api/v1/knowledge-bases/jobs/{first['job_id']}/cancel", headers=owner)).status_code == 200
            await _drain(sessions)
            assert (await client.get(f"/api/v1/knowledge-bases/jobs/{first['job_id']}", headers=owner)).json()["data"][
                "state"
            ] == KnowledgeJobState.CANCELLED
            rebuilt = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            assert rebuilt.status_code == 202
            target = rebuilt.json()["data"]
            kb["revision"] = target["revision"]
            async with sessions.begin() as db:
                job = await db.get(KnowledgeJob, target["job_id"])
                job.state, job.attempts, job.lease_token = KnowledgeJobState.PARSING, 1, "lost-host"
                job.lease_expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
            await _drain(sessions)
            state = (await client.get(f"/api/v1/knowledge-bases/jobs/{target['job_id']}", headers=owner)).json()["data"]
            assert state["state"] == KnowledgeJobState.COMPLETED and state["attempts"] == 2
            assert not (await _search(client, owner, kb["id"], "知识租约恢复"))["no_answer"]

    asyncio.run(exercise())


@pytest.mark.parametrize("invalid", [[[0.0, 0.0]], [[float("nan"), 1]], [[True, 1]], [[1]], [], [[1, 2], [3, 4]]])
def test_embedding_rejects_invalid_vectors(invalid) -> None:
    """非有限、零范数、布尔、数量及维度不匹配全部拒绝。"""
    with pytest.raises(KnowledgeProcessingError):
        normalize_vectors(invalid, 1, 2)


def test_embedding_http_protocol_errors_and_shape() -> None:
    """独立适配验证真实 HTTP 形状、无截断、限流、鉴权及超时脱敏。"""
    model = ModelCatalog(id=2, external_id="embeddinggemma")
    provider = ProviderConnection(kind=ProviderKind.OLLAMA, base_url="http://embedding.test/v1", timeout_seconds=3)

    async def exercise():
        """模拟最底层 HTTP，保持正式响应解析与向量校验。"""
        for status, expected in [
            (401, KnowledgeFailure.AUTHENTICATION),
            (429, KnowledgeFailure.RATE_LIMIT),
            (500, KnowledgeFailure.UPSTREAM),
        ]:

            async def fail(request):
                """错误正文含敏感文本，不能流入公开异常。"""
                return httpx.Response(status, text="secret upstream credential")

            async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
                with pytest.raises(KnowledgeProcessingError) as failure:
                    await embed_texts(model, provider, ["查询"], dimensions=2, client=client)
                assert failure.value.code == expected and "secret" not in str(failure.value)

        async def success(request):
            """检查 Ollama 嵌入协议与禁用静默截断。"""
            payload = json.loads(request.content)
            assert request.url.path == "/api/embed" and payload["truncate"] is False
            return httpx.Response(
                200,
                json={
                    "embeddings": [[3, 4]],
                    "prompt_eval_count": 2,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as client:
            result = await embed_texts(model, provider, ["查询"], dimensions=2, client=client)
        assert result.vectors == [[0.6, 0.8]] and result.tokens == 2

        async def timeout(request):
            """触发真实 httpx 超时类别。"""
            raise httpx.ReadTimeout("secret", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with pytest.raises(KnowledgeProcessingError) as failure:
                await embed_texts(model, provider, ["查询"], dimensions=2, client=client)
            assert failure.value.code == KnowledgeFailure.TIMEOUT

    asyncio.run(exercise())


def test_hybrid_fixed_space_budget_and_revocation_during_embedding(tmp_path, monkeypatch) -> None:
    """混合召回绑定固定模型空间，预算拒绝保留旧代，嵌入等待时撤权不泄漏结果。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def fake_embedding(model, provider, texts, *, dimensions):
        """受控单位向量只测试融合和事务，不能当作真实上游验收。"""
        return EmbeddingResult([[1.0, 0.0] for _ in texts], len(texts), None)

    monkeypatch.setattr(knowledge_jobs, "embed_texts", fake_embedding)
    monkeypatch.setattr(knowledge_service, "embed_texts", fake_embedding)

    async def exercise():
        """分别核对成本保护、共享撤销与模型身份变化。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            alice, bob = headers
            await _seed_models(sessions)
            kb = await _create_kb(client, alice, search_mode="hybrid", embedding_model_id=2)
            await _upload(client, alice, kb, "制度.txt", "差旅报销提交期限为十五个工作日。".encode())
            await _drain(sessions)
            result = await _search(client, alice, kb["id"], "差旅报销期限", rerank="coverage")
            assert len(result["hits"]) == 1
            old_index = result["hits"][0]["index_id"]
            assert (
                await client.post(
                    f"/api/v1/knowledge-bases/{kb['id']}/shares",
                    headers=alice,
                    json={
                        "user_id": 2,
                    },
                )
            ).status_code == 200
            started, continue_embedding = asyncio.Event(), asyncio.Event()

            async def waiting(model, provider, texts, *, dimensions):
                """模拟远端等待，允许中途撤销知识库授权。"""
                started.set()
                await continue_embedding.wait()
                return await fake_embedding(model, provider, texts, dimensions=dimensions)

            monkeypatch.setattr(knowledge_service, "embed_texts", waiting)
            pending = asyncio.create_task(search_knowledge(sessions, 2, [old_index], KnowledgeSearchIn(query="报销")))
            await started.wait()
            assert (await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/shares/2", headers=alice)).status_code == 200
            continue_embedding.set()
            assert (await pending)["no_answer"]
            monkeypatch.setattr(knowledge_service, "embed_texts", fake_embedding)
            changed = await client.put(
                f"/api/v1/knowledge-bases/{kb['id']}",
                headers=alice,
                json={
                    "name": "预算不足",
                    "expected_revision": kb["revision"],
                    "search_mode": "hybrid",
                    "embedding_model_id": 2,
                    "embedding_token_budget": 1,
                },
            )
            assert changed.status_code == 200, changed.text
            await _drain(sessions)
            state = (await client.get(f"/api/v1/knowledge-bases/jobs/{changed.json()['data']['job_id']}", headers=alice)).json()["data"]
            assert state["failure_code"] == KnowledgeFailure.BUDGET_EXCEEDED
            assert (await _search(client, alice, kb["id"], "报销"))["hits"][0]["index_id"] == old_index
            async with sessions.begin() as db:
                model = await db.get(ModelCatalog, 2)
                model.external_id = "other-space"
            failed = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/search",
                headers=alice,
                json={
                    "query": "报销",
                },
            )
            assert failed.status_code == 409
            assert not (await _search(client, alice, kb["id"], "报销", keyword_only=True))["no_answer"]

    asyncio.run(exercise())


class KnowledgeModel(FakeMessagesListChatModel):
    """通过真实运行图调用已注册知识检索工具。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """接收正式 Registry 工具并保持预定消息序列。"""
        return self


def test_agent_shared_permissions_run_snapshot_and_retrieval_audit(tmp_path, monkeypatch) -> None:
    """共享 Agent 不共享知识库；显式授权后固定代、记录审计且 SSE 不含原文。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise():
        """用正式会话接口和 DeepAgents 执行器验证授权与检索闭环。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            alice, bob = headers
            await _seed_models(sessions)
            kb = await _create_kb(client, alice)
            await _upload(client, alice, kb, "制度.txt", "报销机密期限为十五个工作日。".encode())
            await _drain(sessions)
            created = await client.post(
                "/api/v1/agents",
                headers=alice,
                json={
                    "name": "知识助手",
                    "default_model_id": 1,
                    "knowledge_base_ids": [kb["id"]],
                },
            )
            assert created.status_code == 201, created.text
            agent_id = created.json()["data"]["id"]
            assert created.json()["data"]["knowledge_base_ids"] == [kb["id"]]
            assert (
                await client.post(
                    f"/api/v1/agents/{agent_id}/shares",
                    headers=alice,
                    json={
                        "user_id": 2,
                    },
                )
            ).status_code == 200
            session = await client.post(
                f"/api/v1/agents/{agent_id}/sessions",
                headers=bob,
                json={
                    "title": "检索验证",
                },
            )
            assert session.status_code == 201
            base = f"/api/v1/agents/{agent_id}/sessions/{session.json()['data']['id']}"
            submitted = await client.post(
                f"{base}/runs",
                headers=bob,
                json={
                    "input_text": "报销期限？",
                    "request_key": "knowledge-no-access",
                },
            )
            assert submitted.status_code == 201, submitted.text
            run_id = submitted.json()["data"]["id"]
            async with sessions.begin() as db:
                run = await claim_run(db, run_id)
            async with sessions() as db:
                tools, _ = await make_worker_tool_resolver(sessions)(db, run)
                assert "search_knowledge_base" not in {tool.name for tool in tools}
                assert not (await db.scalars(select(RunKnowledgeBase).where(RunKnowledgeBase.run_id == run_id))).all()
            from app.services.run_manager import fail_run

            async with sessions.begin() as db:
                await fail_run(db, run_id, run.lease_token, "runtime_error")
            assert (
                await client.post(
                    f"/api/v1/knowledge-bases/{kb['id']}/shares",
                    headers=alice,
                    json={
                        "user_id": 2,
                    },
                )
            ).status_code == 200
            submitted = await client.post(
                f"{base}/runs",
                headers=bob,
                json={
                    "input_text": "报销期限？",
                    "request_key": "knowledge-allowed",
                },
            )
            assert submitted.status_code == 201, submitted.text
            run_id = submitted.json()["data"]["id"]
            old_index = (await _search(client, bob, kb["id"], "报销期限"))["hits"][0]["index_id"]
            listed = (await client.get(f"/api/v1/knowledge-bases/{kb['id']}/documents", headers=alice)).json()["data"]
            await _upload(client, alice, kb, "制度.txt", "报销机密期限改为三十个工作日。".encode(), document_id=listed[0]["id"])
            await _drain(sessions)
            assert (await _search(client, bob, kb["id"], "报销期限"))["hits"][0]["index_id"] != old_index
            model = KnowledgeModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "knowledge-1",
                                "name": "search_knowledge_base",
                                "args": {
                                    "query": "报销期限",
                                },
                            }
                        ],
                    ),
                    AIMessage(content="应在十五个工作日内提交，请核对返回的知识引用。"),
                ]
            )
            result = await execute_text_run(
                sessions, run_id, model, tmp_path / "checkpoints.db", resolve_tools=make_worker_tool_resolver(sessions)
            )
            assert result.state == "completed", result.failure_code
            async with sessions() as db:
                audit = await db.scalar(select(KnowledgeRetrieval).where(KnowledgeRetrieval.run_id == run_id))
                assert audit.hit_count == 1 and audit.references[0]["knowledge_base_id"] == kb["id"]
                assert audit.references[0]["index_id"] == old_index and audit.references[0]["document_version"] == 1
                assert "报销" not in audit.query_hash and "content" not in json.dumps(audit.references)
            audit_api = await client.get(f"/api/v1/runs/{run_id}/knowledge-retrievals", headers=bob)
            assert audit_api.status_code == 200 and audit_api.json()["data"][0]["hit_count"] == 1
            assert "报销机密" not in audit_api.text
            assert (await client.get(f"/api/v1/runs/{run_id}/knowledge-retrievals", headers=alice)).status_code == 404
            snapshot = (await client.get(f"/api/v1/runs/{run_id}/snapshot", headers=bob)).json()["data"]
            tool_events = [event for event in snapshot["events"] if event["event_type"].startswith("tool_")]
            assert tool_events and "报销机密" not in json.dumps(tool_events, ensure_ascii=False)
            pending = await client.post(
                f"{base}/runs",
                headers=bob,
                json={
                    "input_text": "报销期限？",
                    "request_key": "knowledge-revoked",
                },
            )
            assert pending.status_code == 201
            pending_id = pending.json()["data"]["id"]
            assert (await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/shares/2", headers=alice)).status_code == 200
            snapshot = (await client.get(f"/api/v1/runs/{pending_id}/snapshot", headers=bob)).json()["data"]
            assert snapshot["run"]["state"] == "cancelled"

    asyncio.run(exercise())
