# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 10:20:03
# @Author  : yangyuexiong
# @File    : test_knowledge_formats.py

"""验证第三阶段格式路由、结构证据、资源边界及正式上传检索链路。"""

import asyncio
import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_config
from app.core.enums import KnowledgeContentRole, KnowledgeFailure, KnowledgeJobState, KnowledgeSourceKind
from app.models.knowledge import KnowledgeChunk, KnowledgeDocumentVersion, KnowledgeJob
from app.services import knowledge_enrichment, knowledge_jobs
from app.services.artifact_store import ArtifactValidationError, validate_file
from app.services.knowledge_formats import KNOWLEDGE_EXTRA_MEDIA_TYPES
from app.services.knowledge_parser import KnowledgeProcessingError, chunk_knowledge, parse_knowledge, parse_knowledge_isolated
from scripts.generate_knowledge_phase3_fixtures import phase3_fixtures
from tests.test_knowledge import _create_kb, _drain, _search, _upload
from tests.test_knowledge_processing import TableExplanationModel, _configure_model
from tests.test_m51_governance import _database, _http_platform

EVALUATION = json.loads((Path(__file__).parent / "fixtures" / "m64_format_retrieval.json").read_text())


@pytest.fixture(scope="module")
def format_files() -> dict[str, bytes]:
    """固定使用标准 Office 库生成的 PPTX 与同一套配置/HTML 样例。"""
    return phase3_fixtures()


@pytest.mark.parametrize("filename", ["policy.html", "policy.json", "policy.yaml", "policy.pptx"])
def test_routed_formats_exact_sources_and_atomic_records(format_files, filename: str) -> None:
    """各格式保留精确原文切片和语义边界，不跨配置记录、幻灯片或备注合并。"""
    parsed = parse_knowledge(format_files[filename], KNOWLEDGE_EXTRA_MEDIA_TYPES[Path(filename).suffix])
    chunks = chunk_knowledge(parsed, 200, 20)
    assert chunks and parsed.metadata["parser"] == "structured-formats-v1"
    for chunk in chunks:
        assert chunk["content"] == parsed.text[chunk["start_offset"] : chunk["end_offset"]]
        assert chunk["source_locator"]["kind"] in {KnowledgeSourceKind.HTML, KnowledgeSourceKind.CONFIG, KnowledgeSourceKind.SLIDE}
    for chunk in chunks:
        if "甲供应商" in chunk["content"]:
            assert "3800" in chunk["content"] and "50" in chunk["content"] and "乙供应商" not in chunk["content"]
    if filename == "policy.html":
        assert "差旅报销制度" in parsed.text and "须在十五个工作日内" in parsed.text
        assert "    approve = True" in parsed.text
        assert not any(term in parsed.text for term in EVALUATION["negative"])
        assert parsed.metadata["selection"] == "main" and parsed.metadata["excluded_regions"] > 0
        assert any(chunk["source_locator"].get("element_id") == "travel" for chunk in chunks)
    if filename == "policy.json":
        assert "/a~1b/~0key" in parsed.text and '"${POLICY_ENDPOINT}"' in parsed.text
        assert any(chunk["source_locator"]["pointer"] == "/suppliers/0" for chunk in chunks)
    if filename == "policy.yaml":
        assert '/service/on（str）= "保留字符串键"' in parsed.text
        assert '/service/launch_date（str）= "2026-11-13"' in parsed.text
        assert "/backup/retry_count" in parsed.text
        assert all(chunk["source_locator"]["line_start"] is not None for chunk in chunks)
    if filename == "policy.pptx":
        assert parsed.text.startswith("提交期限")
        assert parsed.metadata["slide_count"] == 3 and parsed.metadata["hidden_slides"] == 1
        assert parsed.metadata["notes_blocks"] == 2 and parsed.metadata["unsupported_visual_objects"] == 1
        assert not any(term in parsed.text for term in EVALUATION["negative"])
        notes = [chunk for chunk in chunks if chunk["source_locator"]["role"] == KnowledgeContentRole.NOTES]
        assert len(notes) == 2 and all("讲解备注" in chunk["content"] for chunk in notes)
        assert {chunk["page"] for chunk in chunks} == {1, 2}


@pytest.mark.parametrize("filename", ["duplicate.json", "duplicate.yaml", "cycle.yaml", "merge.yaml", "unsafe-tag.yaml"])
def test_invalid_configuration_does_not_silently_publish(format_files, filename: str) -> None:
    """重复键、循环别名、合并键及任意标签均通过正式子进程返回结构错误。"""
    with pytest.raises(KnowledgeProcessingError) as error:
        asyncio.run(parse_knowledge_isolated(format_files[filename], KNOWLEDGE_EXTRA_MEDIA_TYPES[Path(filename).suffix]))
    assert error.value.code == KnowledgeFailure.INVALID_STRUCTURE


@pytest.mark.parametrize(
    "content,media_type,failure",
    [
        (b'{"x":NaN}', "application/json", KnowledgeFailure.INVALID_STRUCTURE),
        (b"value: .inf", "application/yaml", KnowledgeFailure.INVALID_STRUCTURE),
        (b"---\na: 1\n---\na: 2", "application/yaml", KnowledgeFailure.INVALID_STRUCTURE),
        (b"1: value", "application/yaml", KnowledgeFailure.INVALID_STRUCTURE),
        (b"[" * 60 + b"0" + b"]" * 60, "application/json", KnowledgeFailure.SIZE_LIMIT),
        (
            json.dumps(
                {
                    "long": "x" * 9000,
                },
            ).encode(),
            "application/json",
            KnowledgeFailure.SIZE_LIMIT,
        ),
        (("<div>" * 60 + "text" + "</div>" * 60).encode(), "text/html", KnowledgeFailure.SIZE_LIMIT),
    ],
)
def test_structure_limits_and_non_finite_values(content: bytes, media_type: str, failure: KnowledgeFailure) -> None:
    """明确拒绝超深、超长单值及不确定配置，避免返回部分成功内容。"""
    with pytest.raises(KnowledgeProcessingError) as error:
        asyncio.run(parse_knowledge_isolated(content, media_type))
    assert error.value.code == failure


@pytest.mark.parametrize(
    "html,expected",
    [
        ("<article><header><h1>业务标题</h1></header><p>应在<strong>十五个工作日</strong>内提交</p></article>", "应在十五个工作日内提交"),
        ("<body><nav>噪声</nav><div>直接<strong>行内文字</strong>不拆开</div></body>", "直接行内文字不拆开"),
        ("<main><p>第一段<p>第二段</main>", "第一段\n第二段"),
        ('<div role="main"><header><h1>角色主文标题</h1></header><p>主文</p></div>', "角色主文标题"),
        ("<main><p><span>Hotel </span><b>allowance</b> remains unchanged.</p></main>", "Hotel allowance remains unchanged."),
    ],
)
def test_html_main_fallback_and_inline_text(html: str, expected: str) -> None:
    """主文回退不重复正文，文章头标题保留，行内格式不切断连续中文。"""
    parsed = parse_knowledge(html.encode(), "text/html")
    assert expected in parsed.text and "噪声" not in parsed.text


def test_yaml_alias_expansion_is_bounded() -> None:
    """合法非循环别名也按实际展开数计量，阻止指数重复知识撑满索引。"""
    lines = ["base: &a [value]"]
    for index in range(1, 8):
        previous, current = chr(ord("a") + index - 1), chr(ord("a") + index)
        lines.append(f"level{index}: &{current} [" + ", ".join(f"*{previous}" for _ in range(8)) + "]")
    with pytest.raises(KnowledgeProcessingError) as error:
        asyncio.run(parse_knowledge_isolated("\n".join(lines).encode(), "application/yaml"))
    assert error.value.code == KnowledgeFailure.SIZE_LIMIT


@pytest.mark.parametrize("filename", ["policy.html", "policy.json", "policy.yaml", "policy.pptx"])
def test_new_formats_are_knowledge_uploads_only(tmp_path, format_files, filename: str) -> None:
    """扩展知识文件不会开放普通聊天附件，Office 仍验证容器和宏限制。"""
    path = tmp_path / filename
    path.write_bytes(format_files[filename])
    with pytest.raises(ArtifactValidationError):
        validate_file(path, filename)
    assert validate_file(path, filename, allow_knowledge=True) == KNOWLEDGE_EXTRA_MEDIA_TYPES[path.suffix]


@pytest.mark.parametrize("variant", ["doctype", "missing-target", "traversal", "duplicate-relation", "macro"])
def test_pptx_invalid_parts_and_external_boundaries(format_files, variant: str) -> None:
    """拒绝声明实体、缺失/越界关系、重复关系或宏，不读取包外文件。"""
    original = zipfile.ZipFile(io.BytesIO(format_files["policy.pptx"]))
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for name in original.namelist():
            data = original.read(name)
            if name == "ppt/presentation.xml" and variant == "doctype":
                data = b'<!DOCTYPE p [<!ENTITY value "not-executed">]>' + data
            if name == "ppt/_rels/presentation.xml.rels" and variant in {"missing-target", "traversal"}:
                data = data.replace(b'Target="slides/slide1.xml"', b'Target="missing.xml"' if variant == "missing-target" else b'Target="../../etc/passwd"')
            if name == "ppt/_rels/presentation.xml.rels" and variant == "duplicate-relation":
                data = data.replace(b"</Relationships>", b'<Relationship Id="rId1" Target="slides/slide1.xml" Type="slide"/></Relationships>')
            target.writestr(name, data)
        if variant == "macro":
            target.writestr("ppt/vbaProject.bin", b"not executed")
    with pytest.raises(KnowledgeProcessingError) as error:
        asyncio.run(parse_knowledge_isolated(output.getvalue(), KNOWLEDGE_EXTRA_MEDIA_TYPES[".pptx"]))
    assert error.value.code == KnowledgeFailure.INVALID_STRUCTURE


@pytest.mark.parametrize("filename", ["policy.html", "policy.json", "policy.yaml", "policy.pptx"])
def test_new_format_http_worker_retrieval_cache_and_revocation(tmp_path, monkeypatch, format_files, filename: str) -> None:
    """正式上传与索引后运行固定检索样本，预览不重新解析，撤权阻断来源。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))

    async def exercise() -> None:
        """在隔离原生 FTS 中核对目标词和去噪效果，保留权限和发布代约束。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner, other = headers
            kb = await _create_kb(client, owner, processing_mode="auto")
            uploaded = await _upload(client, owner, kb, filename, format_files[filename])
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED, job.failure_code
                assert job.processing_call_count == job.reserved_tokens == 0
                version = await db.scalar(select(KnowledgeDocumentVersion))
                chunks = (await db.scalars(select(KnowledgeChunk))).all()
                assert all(chunk.content == version.extracted_text[chunk.start_offset : chunk.end_offset] for chunk in chunks)
            for case in EVALUATION["cases"]:
                if case["filename"] != filename:
                    continue
                hits = (await _search(client, owner, kb["id"], case["query"]))["hits"]
                assert any(case["contains"] in hit["content"] and hit["source_locator"]["kind"] == case["kind"] for hit in hits)
            for term in EVALUATION["negative"]:
                assert not (await _search(client, owner, kb["id"], term))["hits"]
            hit = (await _search(client, owner, kb["id"], "财务部"))["hits"][0]
            citation_url = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}"
            preview_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{uploaded['document']['id']}/processing-preview"
            preview = await client.get(preview_url, headers=owner)
            assert preview.status_code == 200 and preview.headers["cache-control"] == "no-store"
            assert preview.json()["data"]["processing_metadata"]["format"] == Path(filename).suffix.lstrip(".")
            assert (await client.get(citation_url, headers=other)).status_code == 404
            assert (await client.get(preview_url, headers=other)).status_code == 404
            shared = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/shares",
                headers=owner,
                json={
                    "user_id": 2,
                },
            )
            assert shared.status_code == 200
            assert (await client.get(citation_url, headers=other)).status_code == 200
            assert (await client.delete(f"/api/v1/knowledge-bases/{kb['id']}/shares/2", headers=owner)).status_code == 200
            assert (await client.get(citation_url, headers=other)).status_code == 404

            async def forbidden(*args, **kwargs):
                """同配置重建和预览只能使用已缓存解析，不重新处理上传文件。"""
                raise AssertionError("相同指纹不应重新解析")

            monkeypatch.setattr(knowledge_jobs, "parse_knowledge_isolated", forbidden)
            rebuilt = await client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/rebuild",
                headers=owner,
                json={
                    "expected_revision": kb["revision"],
                },
            )
            assert rebuilt.status_code == 202
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, rebuilt.json()["data"]["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED
            assert (await client.get(preview_url, headers=owner)).status_code == 200

    asyncio.run(exercise())


@pytest.mark.parametrize("filename", ["policy.html", "policy.json", "policy.yaml", "policy.pptx"])
@pytest.mark.parametrize("mode", ["auto", "original", "enhanced"])
def test_new_format_enrichment_is_explicit_and_evidence_stays_original(tmp_path, monkeypatch, format_files, filename: str, mode: str) -> None:
    """新增格式自动/原文不调用加工模型，增强模式按证据解释并独立记录用量。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    model = TableExplanationModel()
    monkeypatch.setattr(knowledge_enrichment, "build_chat_model", lambda *args, **kwargs: model)

    async def exercise() -> None:
        """通过实际后台链路检查策略，避免只测试前端标签或条件分支。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            await _configure_model(sessions)
            kb = await _create_kb(client, headers[0], processing_mode=mode, processing_model_id=1)
            uploaded = await _upload(client, headers[0], kb, filename, format_files[filename])
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, uploaded["job_id"])
                assert job.state == KnowledgeJobState.COMPLETED, job.failure_code
                assert bool(model.calls) == (mode == "enhanced")
                assert job.processing_call_count == model.calls
                version = await db.scalar(select(KnowledgeDocumentVersion))
                chunks = (await db.scalars(select(KnowledgeChunk))).all()
                assert all(chunk.content == version.extracted_text[chunk.start_offset : chunk.end_offset] for chunk in chunks)
                assert all(bool(chunk.knowledge_notes) == (mode == "enhanced") for chunk in chunks)
                if mode == "enhanced":
                    assert job.processing_tokens == 120 * model.calls and job.processing_usage

    asyncio.run(exercise())


def test_configuration_mixed_array_keeps_index_order() -> None:
    """混合类型数组按原始下标顺序形成证据，不把跨对象的标量合并。"""
    parsed = parse_knowledge(b'[1,{"amount":2},3]', "application/json")
    chunks = chunk_knowledge(parsed, 800, 100)
    assert [chunk["source_locator"]["paths"] for chunk in chunks] == [["/0"], ["/1/amount"], ["/2"]]


@pytest.mark.parametrize("filename", ["policy.html", "policy.pptx"])
def test_explicit_table_headers_stay_with_data_rows(format_files, filename: str) -> None:
    """行级检索携带显式表头，避免金额和数量只有位置而缺少业务含义。"""
    parsed = parse_knowledge(format_files[filename], KNOWLEDGE_EXTRA_MEDIA_TYPES[Path(filename).suffix])
    record = next(chunk for chunk in chunk_knowledge(parsed, 200, 20) if "甲供应商" in chunk["content"])
    assert "列1（供应商）=甲供应商" in record["content"]
    assert "列2（单价）=3800" in record["content"] and "列3（数量）=50" in record["content"]


@pytest.mark.parametrize("wrapper", ["", "<div><section>{}</section></div>"])
def test_html_nested_table_is_not_flattened_into_parent(wrapper: str) -> None:
    """直接和多层包装的嵌套表格都拒绝混为父表的一行。"""
    inner = "<table><tr><td>内表数据</td></tr></table>"
    content = f"<main><table><tr><td>{wrapper.format(inner) if wrapper else inner}</td></tr></table></main>"
    with pytest.raises(KnowledgeProcessingError) as error:
        parse_knowledge(content.encode(), "text/html")
    assert error.value.code == KnowledgeFailure.INVALID_STRUCTURE


@pytest.mark.parametrize("value", ["!!int invalid", "!!float 1e999", "!!bool invalid"])
def test_yaml_explicit_scalar_tags_must_have_valid_values(value: str) -> None:
    """显式基础类型也校验实际值，不发布伪装成数字或布尔的配置。"""
    with pytest.raises(KnowledgeProcessingError) as error:
        parse_knowledge(f"value: {value}".encode(), "application/yaml")
    assert error.value.code == KnowledgeFailure.INVALID_STRUCTURE


def test_hidden_slide_group_excludes_descendants(format_files) -> None:
    """隐藏分组的全部后代均不进入标题、正文或备注。"""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation(io.BytesIO(format_files["policy.pptx"]))
    group = presentation.slides[0].shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Inches(1)).text = "隐藏分组机密标记"
    group._element.xpath("./p:nvGrpSpPr/p:cNvPr")[0].set("hidden", "1")
    output = io.BytesIO()
    presentation.save(output)
    parsed = parse_knowledge(output.getvalue(), KNOWLEDGE_EXTRA_MEDIA_TYPES[".pptx"])
    assert "隐藏分组机密标记" not in parsed.text and "十五个工作日" in parsed.text


@pytest.mark.parametrize("filename", ["policy.html", "policy.json", "policy.yaml", "policy.pptx"])
def test_failed_format_update_retains_published_preview_and_delete_revokes(tmp_path, monkeypatch, format_files, filename: str) -> None:
    """真实结构错误不能替换旧活动代或预览摘要，删除立即阻断旧引用。"""
    monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "files"))
    invalid = {
        "policy.html": b"<main><table><tr><td><table><tr><td>invalid</td></tr></table></td></tr></table></main>",
        "policy.json": format_files["duplicate.json"],
        "policy.yaml": format_files["duplicate.yaml"],
    }
    if filename == "policy.pptx":
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(format_files[filename])) as original, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
            for name in original.namelist():
                data = original.read(name)
                if name == "ppt/slides/slide1.xml":
                    data = b'<!DOCTYPE p [<!ENTITY forbidden "not-executed">]>' + data
                target.writestr(name, data)
        invalid[filename] = output.getvalue()

    async def exercise() -> None:
        """通过正式上传、后台处理、预览和删除接口核对不可变发布快照。"""
        async with _database(tmp_path) as sessions, _http_platform(sessions) as (client, headers):
            owner = headers[0]
            kb = await _create_kb(client, owner)
            uploaded = await _upload(client, owner, kb, filename, format_files[filename])
            await _drain(sessions)
            hit = (await _search(client, owner, kb["id"], "财务部"))["hits"][0]
            document_id = uploaded["document"]["id"]
            preview_url = f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}/processing-preview"
            citation_url = f"/api/v1/knowledge-bases/{kb['id']}/citations/{hit['chunk_id']}"
            old_preview = (await client.get(preview_url, headers=owner)).json()["data"]
            broken = await _upload(client, owner, kb, filename, invalid[filename], document_id=document_id)
            await _drain(sessions)
            async with sessions() as db:
                job = await db.get(KnowledgeJob, broken["job_id"])
                assert job.state == KnowledgeJobState.FAILED and job.failure_code == KnowledgeFailure.INVALID_STRUCTURE
            current = (await _search(client, owner, kb["id"], "财务部"))["hits"][0]
            assert current["index_id"] == hit["index_id"] and current["document_version"] == 1
            preview = (await client.get(preview_url, headers=owner)).json()["data"]
            assert preview["current_version"] == 2 and old_preview["current_version"] == 1
            assert {key: value for key, value in preview.items() if key != "current_version"} == {
                key: value for key, value in old_preview.items() if key != "current_version"
            }
            assert (await client.get(citation_url, headers=owner)).status_code == 200
            removed = await client.delete(
                f"/api/v1/knowledge-bases/{kb['id']}/documents/{document_id}?expected_revision={kb['revision']}",
                headers=owner,
            )
            assert removed.status_code == 200, removed.text
            assert not (await _search(client, owner, kb["id"], "财务部"))["hits"]
            assert (await client.get(citation_url, headers=owner)).status_code == 404
            assert (await client.get(preview_url, headers=owner)).status_code == 404
            await _drain(sessions)

    asyncio.run(exercise())
