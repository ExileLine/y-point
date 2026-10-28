# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 15:30:00
# @Author  : yangyuexiong
# @File    : test_image_context.py

"""验证受限图片上传与会话视觉上下文的授权和恢复边界。"""

import asyncio
import os
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from deepagents import create_deep_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage, get_buffer_string
from PIL import Image
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_config
from app.core.password import hash_password
from app.db.session import get_db_session
from app.main import app
from app.models.admin import Admin
from app.models.base import Base
from app.models.artifact import Artifact
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.services.run_dispatcher import dispatch_next_run
from app.services.runtime_checkpoint import open_async_checkpointer
from app.services.artifact_store import ArtifactValidationError, artifact_root, storage_path, validate_file
from app.services.run_executor import _summary_messages_without_images


class VisionModel(FakeMessagesListChatModel):
    """允许假模型接入视觉会话的工具绑定接口。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """兼容 Agent 的工具绑定接口。"""
        return self


def test_image_validation_and_summary_redaction(tmp_path: Path) -> None:
    """拒绝截断图片，摘要历史不包含检查点中的图片正文。"""
    image = BytesIO()
    Image.new("RGB", (16, 16), color="red").save(image, format="PNG")
    path = tmp_path / "image.png"
    path.write_bytes(image.getvalue()[:-12])
    with pytest.raises(ArtifactValidationError):
        validate_file(path, path.name)

    secret = "data:image/png;base64,SECRET_IMAGE_CONTENT"
    history = [HumanMessage(content=[
        {"type": "text", "text": "Describe the picture"},
        {"type": "text", "text": "Reference image.png"},
        {"type": "image_url", "image_url": {"url": secret}},
    ])]
    summary_input = get_buffer_string(_summary_messages_without_images(history))
    assert "Describe the picture" in summary_input
    assert "Reference image.png" in summary_input
    assert "SECRET_IMAGE_CONTENT" not in summary_input
    assert secret in str(history[0].content)

@pytest.mark.parametrize("live", [
    False,
    pytest.param(True, marks=pytest.mark.skipif(
        os.environ.get("YPOINT_TEST_OLLAMA_M4_IMAGE") != "1", reason="需显式启用真实图片验收",
    )),
])
def test_image_context_requires_vision_and_rechecks_file(tmp_path: Path, monkeypatch, live: bool) -> None:
    """图片仅交给已验证视觉模型，文件删除后运行明确失败。"""

    async def exercise() -> None:
        """通过正式 HTTP 接口与隔离 worker 执行图片会话。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'images.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(get_config(), "ARTIFACT_STORAGE_DIR", str(tmp_path / "artifacts"))

        async def test_session():
            """将请求绑定到隔离业务库。"""
            async with sessions() as db:
                yield db

        app.dependency_overrides[get_db_session] = test_session
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add_all([
                    Admin(id=1, username="vision-owner", password=hash_password("StrongPassword123"),
                          role="user", status=1, is_deleted=0),
                    ProviderConnection(id=1, name="vision-provider", name_key="vision-provider", kind="ollama",
                                       base_url="http://127.0.0.1:11434/v1", creator_id=1, status=1, is_deleted=0),
                    ModelCatalog(id=1, provider_id=1,
                                 external_id="gemma4:31b-cloud" if live else "vision-test", display_name="Vision test",
                                 verified_capabilities=["text"], context_window=8192,
                                 creator_id=1, status=1, is_deleted=0),
                ])

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                login = await client.post("/api/v1/auth/login", json={
                    "identifier": "vision-owner", "password": "StrongPassword123",
                })
                headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
                agent = await client.post("/api/v1/agents", headers=headers, json={
                    "name": "Vision", "default_model_id": 1, "max_output_tokens": 256,
                })
                assert agent.status_code == 201, agent.text
                base = f"/api/v1/agents/{agent.json()['data']['id']}/sessions"
                session = await client.post(base, headers=headers, json={"title": "Vision session"})
                assert session.status_code == 201
                base += f"/{session.json()['data']['id']}"
                image = BytesIO()
                Image.new("RGB", (16, 16), color="red").save(image, format="PNG")
                bad = await client.post("/api/v1/artifacts/uploads", headers=headers, files={
                    "files": ("fake.png", b"not an image", "image/png"),
                })
                assert bad.status_code == 400
                uploaded = await client.post("/api/v1/artifacts/uploads", headers=headers, files={
                    "files": ("red.png", image.getvalue(), "image/png"),
                })
                assert uploaded.status_code == 201, uploaded.text
                image_id = uploaded.json()["data"][0]["id"]
                body = {"input_text": "What color is this?", "request_key": "vision-one",
                        "artifact_ids": [image_id], "context_artifact_ids": [image_id]}
                rejected = await client.post(f"{base}/runs", headers=headers, json=body)
                assert rejected.status_code == 409
                async with sessions.begin() as db:
                    model = await db.get(ModelCatalog, 1)
                    model.verified_capabilities = ["text", "vision"]
                created = await client.post(f"{base}/runs", headers=headers, json=body)
                assert created.status_code == 201, created.text
                run_id = created.json()["data"]["id"]

                async def resolve_model(db, run):
                    """返回可观测的假视觉模型。"""
                    return VisionModel(responses=[AIMessage(content="Red")])

                result = await dispatch_next_run(
                    sessions, tmp_path / "checkpoints.db",
                    **({} if live else {"resolve_model": resolve_model}),
                )
                assert result is not None and result.id == run_id and result.state == "completed"
                async with open_async_checkpointer(tmp_path / "checkpoints.db") as checkpointer:
                    graph = create_deep_agent(
                        model=VisionModel(responses=[AIMessage(content="unused")]), checkpointer=checkpointer,
                    )
                    state = await graph.aget_state({"configurable": {"thread_id": session.json()["data"]["thread_id"]}})
                assert "messages" in state.values, tuple(state.values)
                image_urls = [
                    block["image_url"]["url"]
                    for message in state.values["messages"] if message.type == "human"
                    for block in message.content if isinstance(message.content, list)
                    if isinstance(block, dict) and block.get("type") == "image_url"
                ]
                assert len(image_urls) == 1
                assert image_urls[0].startswith("data:image/png;base64,")
                messages = (await client.get(f"{base}/messages", headers=headers)).json()["data"]
                assert messages[0]["attachments"][0]["artifact_id"] == image_id
                assert messages[0]["attachments"][0]["included_in_context"] is True
                assert "base64" not in str(messages)
                if live:
                    assert "red" in messages[-1]["content"].lower()
                    return

                deleted = await client.delete(f"/api/v1/artifacts/{image_id}", headers=headers)
                assert deleted.status_code == 200
                next_run = await client.post(f"{base}/runs", headers=headers, json={
                    "input_text": "Describe again", "request_key": "vision-two",
                })
                assert next_run.status_code == 201
                next_result = await dispatch_next_run(sessions, tmp_path / "checkpoints.db", resolve_model=resolve_model)
                assert next_result is not None and next_result.state == "completed"
                history = (await client.get(f"{base}/messages", headers=headers)).json()["data"]
                assert history[0]["attachments"][0]["available"] is False
                assert (await client.post(f"{base}/runs", headers=headers, json={
                    "input_text": "Use deleted image", "request_key": "vision-three",
                    "artifact_ids": [image_id], "context_artifact_ids": [image_id],
                })).status_code == 404
                another = await client.post("/api/v1/artifacts/uploads", headers=headers, files={
                    "files": ("red-again.png", image.getvalue(), "image/png"),
                })
                assert another.status_code == 201
                another_id = another.json()["data"][0]["id"]
                queued = await client.post(f"{base}/runs", headers=headers, json={
                    "input_text": "Describe this image", "request_key": "vision-corrupt",
                    "artifact_ids": [another_id], "context_artifact_ids": [another_id],
                })
                assert queued.status_code == 201
                async with sessions() as db:
                    stored = await db.get(Artifact, another_id)
                    storage_path(artifact_root(), stored.storage_key).write_bytes(b"tampered")
                failed = await dispatch_next_run(sessions, tmp_path / "checkpoints.db", resolve_model=resolve_model)
                assert failed is not None and failed.state == "failed" and failed.failure_code == "attachment_unavailable"
        finally:
            app.dependency_overrides.pop(get_db_session, None)
            await engine.dispose()

    asyncio.run(exercise())
