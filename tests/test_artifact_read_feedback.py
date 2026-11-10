# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 10:31:49
# @Author  : yangyuexiong
# @File    : test_artifact_read_feedback.py

"""上传附件误用生成产物读取工具时，返回可修正反馈并继续本轮。"""

import asyncio
import io
import json
from decimal import Decimal
from pathlib import Path

from fastapi import UploadFile
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import ModelCapability, ProviderKind, RunEventType, RunState, UserRole
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_session import AgentSession
from app.models.base import Base
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.models.run_artifact_read import RunArtifactRead
from app.services import message_attachments, worker_tools
from app.services.artifact_store import save_upload
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run


class AttachmentReaderModel(FakeMessagesListChatModel):
    """检查附件正文已进入上下文，且错误不会泄漏其他文件的信息。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保留假模型响应，用正式工具解析与图流程验证附件读取纠正。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """核对上传正文和脱敏错误反馈后返回下一条预设模型响应。"""
        assert any(message.type == "human" and "UPLOAD-REFERENCE-CONTENT" in str(message.content) for message in messages)
        failures = [message for message in messages if isinstance(message, ToolMessage) and message.status == "error"]
        if failures:
            feedback = json.loads(failures[-1].content)
            assert feedback["error"] == "generated_artifact_unavailable"
            assert feedback["retryable"] is True
            assert "参考文件内容" in feedback["message"] and "list_session_artifacts" in feedback["message"]
            assert "UPLOAD-REFERENCE-CONTENT" not in failures[-1].content
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


def test_uploaded_attachment_wrong_reader_can_recover(tmp_path: Path, monkeypatch) -> None:
    """真实附件装载、worker、工具错误事件及图流程可完成恢复，不扩大生成产物读取权限。"""

    async def exercise():
        """在隔离数据库与真实 worker 图中验证上传附件误读后的恢复及来源隔离。"""
        root = tmp_path / "artifacts"
        root.mkdir(mode=0o700)
        for module in (message_attachments, worker_tools):
            monkeypatch.setattr(module, "artifact_root", lambda: root)
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reader.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="attachment-reader", password="unused", role=UserRole.USER))
                db.add(AgentConfig(id=1, owner_id=1, name="附件审核", default_model_id=1))
                db.add(
                    ProviderConnection(
                        id=1,
                        name="test",
                        name_key="test",
                        kind=ProviderKind.OLLAMA,
                        base_url="http://localhost:11434/v1",
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    )
                )
                db.add(
                    ModelCatalog(
                        id=1,
                        provider_id=1,
                        external_id="test",
                        display_name="Test",
                        verified_capabilities=[ModelCapability.TEXT, ModelCapability.TOOLS],
                        creator_id=1,
                        status=1,
                        is_deleted=0,
                    )
                )
                session = AgentSession(id="reader-session", owner_id=1, agent_id=1, thread_id="reader-thread", title="审核")
                db.add(session)
                artifact, _ = await save_upload(UploadFile(file=io.BytesIO(b"UPLOAD-REFERENCE-CONTENT"), filename="plan.txt"), 1, root)
                db.add(artifact)
                await db.flush()
                run = await create_run(
                    db,
                    1,
                    agent_id=1,
                    model_id=1,
                    session_id=session.id,
                    thread_id=session.thread_id,
                    agent_config_version=1,
                    agent_system_prompt="审核上传文件",
                    agent_temperature=Decimal("1"),
                    agent_max_output_tokens=512,
                    input_text="请审核附件",
                    max_model_calls=2,
                    max_tool_calls=1,
                )
                message_id = await db.scalar(select(AgentRunMessage.id).where(AgentRunMessage.run_id == run.id))
                db.add(
                    AgentMessageAttachment(
                        message_id=message_id,
                        artifact_id=artifact.id,
                        position=0,
                        filename=artifact.filename,
                        media_type=artifact.media_type,
                        byte_size=artifact.byte_size,
                        included_in_context=True,
                    )
                )
            model = AttachmentReaderModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_generated_artifact",
                                "args": {
                                    "artifact_id": artifact.id,
                                },
                                "id": "wrong-reader",
                            }
                        ],
                    ),
                    AIMessage(content="已使用消息中的上传附件完成审核"),
                ]
            )
            result = await execute_text_run(
                sessions,
                run.id,
                model,
                tmp_path / "checkpoint.db",
                resolve_tools=worker_tools.make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=1,
            )
            assert result.state == RunState.COMPLETED, result.failure_code
            assert result.model_call_count == 2 and result.tool_call_count == 1
            async with sessions() as db:
                assert list(await db.scalars(select(RunArtifactRead).where(RunArtifactRead.run_id == run.id))) == []
                events = list(await db.scalars(select(AgentRunEvent.event_type).where(AgentRunEvent.run_id == run.id)))
                assert events.count(RunEventType.TOOL_FAILED) == 1 and events.count(RunEventType.TOOL_COMPLETED) == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())
