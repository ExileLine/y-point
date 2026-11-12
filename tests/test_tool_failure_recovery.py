# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 23:28:00
# @Author  : yangyuexiong
# @File    : test_tool_failure_recovery.py

"""复现报告工具误选 Skill 后终止运行以及只读失败污染后续会话的问题。"""

import asyncio
import json
import re
from pathlib import Path
from decimal import Decimal
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.custom_exception import CustomException
from app.core.enums import ArtifactOrigin, ArtifactState, RunEventType, RunState, RunTimeoutReason, ToolEffect, ToolInvocationState, ToolKind, Visibility
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_session import AgentSession
from app.models.agent_run import AgentRun
from app.models.agent_run_event import AgentRunEvent
from app.models.artifact import Artifact
from app.models.base import Base
from app.models.skill import RunSkill, Skill, SkillVersion
from app.models.skill_template import SkillTemplate
from app.models.tool import RunTool, ToolInvocation, UserTool, UserToolVersion
from app.services.run_executor import _can_rewind_failed_run, execute_text_run
from app.services.run_manager import claim_run, create_run
from app.services.skill_reports import skill_document_tool
from app.services.runtime_adapter import RuntimeRunConfig
from app.services.tool_registry import ToolContext
from app.services.worker_tools import _read_generated_artifact
from app.services.user_tools import model_tool_name
from tests.test_skills import SkillModel, skill_text


@pytest.mark.parametrize("other_skill_exists", [False, True])
def test_report_wrong_skill_is_correctable_feedback(tmp_path: Path, other_skill_exists: bool) -> None:
    """模型误用不存在或未绑定 Skill ID 时应收到纠正反馈，不能抛出 404 终止整轮。"""

    async def exercise() -> None:
        """通过真实报告工具与有效租约复现生产中的错误 ID 调用。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wrong-skill.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="owner", password="unused"))
                db.add(Skill(id=12, owner_id=1, name="construction-review", description="报告", visibility=Visibility.PRIVATE))
                if other_skill_exists:
                    db.add(Skill(id=1, owner_id=2, name="private-hidden", description="不能公开", visibility=Visibility.PRIVATE))
                run = await create_run(db, 1)
                run.session_id = "report-session"
                db.add(RunSkill(run_id=run.id, skill_id=12, skill_version=3))
                db.add(SkillTemplate(skill_id=12, skill_version=3, template_key="report.docx", artifact_id=str(uuid4()), fields_schema={}))
            async with sessions.begin() as db:
                run = await claim_run(db, run.id)
            context = ToolContext(sessions=sessions, run_id=run.id, owner_id=1, lease_token=run.lease_token)
            tool = skill_document_tool(context)
            result = await tool.ainvoke(
                {
                    "name": "render_skill_document",
                    "id": "wrong-skill-id",
                    "type": "tool_call",
                    "args": {
                        "skill_id": 1,
                        "report_data": {},
                        "filename": "report.docx",
                    },
                },
            )
            assert isinstance(result, ToolMessage) and result.status == "error"
            feedback = json.loads(result.content)
            assert feedback["error"] == "invalid_tool_arguments" and feedback["retryable"] is True
            assert feedback["issues"][0]["path"] == "/skill_id"
            assert feedback["allowed_skills"] == [
                {
                    "skill_id": 12,
                    "skill_version": 3,
                    "templates": ["report.docx"],
                },
            ]
            async with sessions.begin() as db:
                current = await db.get(Skill, 12)
                current.enabled = False
            with pytest.raises(CustomException) as rejected:
                await tool.ainvoke(
                    {
                        "name": "render_skill_document",
                        "id": "revoked-skill-id",
                        "type": "tool_call",
                        "args": {
                            "skill_id": 12,
                            "report_data": {},
                            "filename": "report.docx",
                        },
                    },
                )
            assert rejected.value.status_code == 404
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "effect, invocation_state, scenario, expected",
    [
        (ToolEffect.READ, ToolInvocationState.COMPLETED, "intact", True),
        (ToolEffect.WRITE, ToolInvocationState.COMPLETED, "intact", False),
        (ToolEffect.READ, ToolInvocationState.UNKNOWN, "intact", False),
        (ToolEffect.READ, ToolInvocationState.FAILED, "intact", False),
        (ToolEffect.READ, ToolInvocationState.COMPLETED, "missing_audit", False),
        (ToolEffect.READ, ToolInvocationState.COMPLETED, "changed_hash", False),
        (ToolEffect.READ, ToolInvocationState.COMPLETED, "generated", False),
        (ToolEffect.READ, ToolInvocationState.COMPLETED, "unfinished_report", False),
        (ToolEffect.READ, ToolInvocationState.COMPLETED, "completed_report", False),
    ],
)
def test_failed_report_after_audited_tool_recovery_policy(
    tmp_path: Path,
    effect: ToolEffect,
    invocation_state: ToolInvocationState,
    scenario: str,
    expected: bool,
) -> None:
    """历史失败轮次只完成已审计只读脚本且报告未生成时可回到干净起点，外部写或未知执行仍阻断。"""

    async def exercise() -> None:
        """保存与真实会话相同的 Skill 读取、脚本成功和报告失败事件链。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery-policy.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1)
                run.state = RunState.FAILED
                run.tool_call_count = 4
                db.add(RunSkill(run_id=run.id, skill_id=12, skill_version=3))
                tool = UserTool(id=13, owner_id=1, name="check_report", description="只读审查", kind=ToolKind.SCRIPT, visibility=Visibility.PRIVATE)
                db.add(tool)
                db.add(UserToolVersion(tool_id=13, version=1, content_hash="a" * 64, input_schema={}, effect=effect, requires_approval=False, verified=True))
                db.add(RunTool(run_id=run.id, tool_id=13, tool_version=1, content_hash=("c" if scenario == "changed_hash" else "a") * 64))
                if scenario != "missing_audit":
                    db.add(ToolInvocation(run_id=run.id, call_id=str(uuid4()), tool_id=13, tool_version=1, state=invocation_state, arguments_hash="b" * 64))
                if scenario == "generated":
                    db.add(
                        Artifact(
                            id=str(uuid4()),
                            owner_id=1,
                            run_id=run.id,
                            filename="partial.docx",
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            byte_size=0,
                            sha256="a" * 64,
                            storage_key=str(uuid4()),
                            source=ArtifactOrigin.GENERATED,
                            state=ArtifactState.PENDING,
                        ),
                    )
                names = ["skill:construction-review@v3", "skill:construction-review@v3", model_tool_name(tool), "render_skill_document"]
                for index, name in enumerate(names):
                    call_id = str(uuid4())
                    db.add(
                        AgentRunEvent(
                            run_id=run.id,
                            sequence=2 + index * 2,
                            event_type=RunEventType.TOOL_STARTED,
                            state=RunState.RUNNING,
                            tool_call_id=call_id,
                            tool_name=name,
                        )
                    )
                    if name == "render_skill_document" and scenario == "unfinished_report":
                        continue
                    ending = RunEventType.TOOL_FAILED if name == "render_skill_document" else RunEventType.TOOL_COMPLETED
                    if name == "render_skill_document" and scenario == "completed_report":
                        ending = RunEventType.TOOL_COMPLETED
                    db.add(
                        AgentRunEvent(
                            run_id=run.id,
                            sequence=3 + index * 2,
                            event_type=ending,
                            state=RunState.RUNNING,
                            tool_call_id=call_id,
                            tool_name=name,
                        )
                    )
            async with sessions() as db:
                assert await _can_rewind_failed_run(db, run) is expected
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("has_successful_turn", [False, True])
def test_legacy_failed_report_does_not_poison_following_turn(tmp_path: Path, has_successful_turn: bool) -> None:
    """真实图重现旧报告异常，已有或没有成功轮次时都允许后续新提问，且不重放只读脚本。"""

    async def exercise() -> None:
        """仅以隔离模型和只读业务工具运行真实中间件、审计及检查点恢复链路。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-report.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "checkpoints.db"
        reads = []
        thread_id = str(uuid4())

        async def create_conversation_run(db, text):
            """沿用真实会话提交所需的 Agent 配置快照，不绕过会话线程保护。"""
            return await create_run(
                db,
                1,
                input_text=text,
                agent_id=11,
                session_id="legacy-session",
                thread_id=thread_id,
                agent_config_version=1,
                agent_system_prompt="",
                agent_temperature=Decimal("0.5"),
                agent_max_output_tokens=4096,
            )

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="owner", password="unused"))
                db.add(AgentConfig(id=11, owner_id=1, name="审查助手", default_model_id=1))
                db.add(AgentSession(id="legacy-session", owner_id=1, agent_id=11, thread_id=thread_id, title="历史故障恢复"))
                db.add(Skill(id=12, owner_id=1, name="construction-review", description="审查", visibility=Visibility.PRIVATE))
                db.add(
                    SkillVersion(
                        skill_id=12,
                        version=3,
                        content_hash="a" * 64,
                        files={
                            "SKILL.md": skill_text("construction-review"),
                        },
                        manifest=[],
                    ),
                )
                definition = UserTool(id=13, owner_id=1, name="check_report", description="只读审查", kind=ToolKind.SCRIPT, visibility=Visibility.PRIVATE)
                db.add(definition)
                db.add(
                    UserToolVersion(
                        tool_id=13,
                        version=1,
                        content_hash="a" * 64,
                        input_schema={},
                        effect=ToolEffect.READ,
                        requires_approval=False,
                        verified=True,
                    )
                )
                first = await create_conversation_run(db, "first")
            if has_successful_turn:
                completed = await execute_text_run(
                    sessions,
                    first.id,
                    SkillModel(responses=[AIMessage(content="first answer")]),
                    checkpoint,
                    lease_seconds=10,
                    poll_seconds=0.05,
                )
                assert completed.state == RunState.COMPLETED
                async with sessions.begin() as db:
                    first = await create_conversation_run(db, "legacy report")
            async with sessions.begin() as db:
                db.add(RunSkill(run_id=first.id, skill_id=12, skill_version=3))
                db.add(RunTool(run_id=first.id, tool_id=13, tool_version=1, content_hash="a" * 64))

            @tool(model_tool_name(definition))
            async def read_report() -> str:
                """读取业务材料，只用计数确认恢复没有重复执行。"""
                reads.append("read")
                return "safe business facts"

            @tool
            async def render_skill_document(skill_id: int, report_data: dict, filename: str) -> str:
                """模拟修复前真实报告工具对错误 Skill ID 抛出的资源异常。"""
                raise CustomException(status_code=404, custom_code=10002, detail="Skill 不存在或不可用")

            async def resolver(db, run):
                """为真实图提供持久化已授权只读工具和旧版报告失败行为。"""
                return [read_report, render_skill_document], {}

            broken = await execute_text_run(
                sessions,
                first.id,
                SkillModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": model_tool_name(definition),
                                    "args": {},
                                    "id": "read-business",
                                },
                            ],
                        ),
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "render_skill_document",
                                    "args": {
                                        "skill_id": 1,
                                        "report_data": {},
                                        "filename": "report.docx",
                                    },
                                    "id": "legacy-wrong-skill",
                                },
                            ],
                        ),
                    ],
                ),
                checkpoint,
                resolve_tools=resolver,
                lease_seconds=10,
                poll_seconds=0.05,
            )
            assert broken.state == RunState.FAILED and broken.failure_code == "model_error"
            async with sessions.begin() as db:
                # 已失败的后续请求不改变失败检查点归属，下一轮仍应找回原始安全失败。
                rejected = await create_conversation_run(db, "previous blocked followup")
                rejected.active_thread_id = None
                rejected.state = RunState.FAILED
                rejected.failure_code = "checkpoint_mismatch"
            async with sessions.begin() as db:
                followup = await create_conversation_run(db, "continue")
                db.add(RunSkill(run_id=followup.id, skill_id=12, skill_version=3))
                db.add(RunTool(run_id=followup.id, tool_id=13, tool_version=1, content_hash="a" * 64))
            recovered = await execute_text_run(
                sessions,
                followup.id,
                SkillModel(responses=[AIMessage(content="followup answer")]),
                checkpoint,
                resolve_tools=resolver,
                lease_seconds=10,
                poll_seconds=0.05,
            )
            assert recovered.state == RunState.COMPLETED, recovered.failure_code
            assert reads == ["read"]
            assert recovered.model_call_count == 1 and recovered.tool_call_count == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "tool_name, safe",
    [
        ("read_file", True),
        ("read_generated_artifact", True),
        ("search_knowledge_base", True),
        ("search_knowledge_base_extra", False),
        ("create_document_artifact", False),
        ("user_tool_999_unbound", False),
        ("read_generated_artifact_extra", False),
    ],
)
@pytest.mark.parametrize("ending", [RunEventType.TOOL_COMPLETED, RunEventType.TOOL_FAILED, None])
def test_read_only_builtin_failure_can_rewind(tmp_path: Path, tool_name: str, safe: bool, ending: RunEventType | None) -> None:
    """只读内置工具已结束后可退回干净检查点，写工具、未绑定工具、名称近似和结果未确认仍拦截。"""

    async def exercise() -> None:
        """保存真实会话出现的历史回读及产物读取事件，验证恢复判定。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'builtin-read.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1)
                run.state = RunState.TIMED_OUT
                run.failure_code = RunTimeoutReason.PROVIDER
                run.tool_call_count = 1
                db.add(
                    AgentRunEvent(
                        run_id=run.id,
                        sequence=2,
                        event_type=RunEventType.TOOL_STARTED,
                        state=RunState.RUNNING,
                        tool_name=tool_name,
                        tool_call_id="readonly-call",
                    )
                )
                if ending is not None:
                    db.add(
                        AgentRunEvent(
                            run_id=run.id,
                            sequence=3,
                            event_type=ending,
                            state=RunState.RUNNING,
                            tool_name=tool_name,
                            tool_call_id="readonly-call",
                        )
                    )
            async with sessions() as db:
                assert await _can_rewind_failed_run(db, run) is (safe and ending is not None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_history_read_then_artifact_error_timeout_keeps_conversation_usable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重现真实会话回读历史、产物读取失败和上游超时，验证后续两轮保留成功上下文且不重放失败工具。"""
    observations = []

    class ArtifactTimeoutModel(SkillModel):
        """通过真实摘要路径回读材料，再模拟读取不存在的产物及供应商超时。"""

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            """摘要仅概述材料，普通调用依次产生历史读取、产物读取和上游超时。"""
            if any("压缩成准确的上下文摘要" in str(message.content) for message in messages):
                answer = AIMessage(content="上一轮已确认材料事实：项目代号青山-42。原文可从历史文件回读。")
            elif not observations:
                source = "\n".join(str(message.content) for message in messages)
                path = re.search(r"/conversation_history/[^\s]+\.md", source)
                assert path is not None, "测试必须经过真实摘要生成的历史文件"
                observations.append("history")
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_file",
                            "args": {
                                "file_path": path.group(),
                                "offset": 0,
                                "limit": 12,
                            },
                            "id": "read-history",
                        },
                    ],
                )
            elif observations == ["history"]:
                assert any(isinstance(message, ToolMessage) and message.name == "read_file" and message.status == "success" for message in messages)
                observations.append("artifact")
                answer = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_generated_artifact",
                            "args": {
                                "artifact_id": "artifact_not_a_real_platform_id",
                            },
                            "id": "read-invalid-artifact",
                        },
                    ],
                )
            else:
                errors = [message for message in messages if isinstance(message, ToolMessage) and message.name == "read_generated_artifact"]
                assert errors and errors[-1].status == "error"
                assert json.loads(errors[-1].content)["retryable"] is True
                observations.append("provider_timeout")
                raise TimeoutError("模拟供应商读取超时")
            return ChatResult(generations=[ChatGeneration(message=answer)])

    class FollowupModel(SkillModel):
        """验证两次正常提问继承成功检查点并丢弃超时轮次的工具与输入。"""

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            """断言成功轮次仍在，而失败轮次及内部补救没有带入新一轮。"""
            assert any(isinstance(message, HumanMessage) and "项目代号青山-42" in str(message.content) for message in messages)
            assert all("请读取错误文件并继续" not in str(message.content) for message in messages)
            assert not any(isinstance(message, ToolMessage) for message in messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="你好，项目代号青山-42。"))])

    async def exercise() -> None:
        """使用真实 worker 产物读取、摘要、检查点与执行器验证恢复，不请求真实供应商。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'artifact-timeout.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "artifact-checkpoints.db"
        thread_id = str(uuid4())
        artifact_id = str(uuid4())

        async def submit(db, text):
            """保留正式会话提交的配置快照和独占线程约束。"""
            return await create_run(
                db,
                1,
                input_text=text,
                agent_id=11,
                session_id="artifact-session",
                thread_id=thread_id,
                agent_config_version=1,
                agent_system_prompt="",
                agent_temperature=Decimal("0.5"),
                agent_max_output_tokens=4096,
            )

        async def resolver(db, run):
            """提供正式产物读取工具，历史读取仍由内置授权中间件控制。"""
            return [_read_generated_artifact(ToolContext(sessions, run.id, run.owner_id, run.lease_token))], {}

        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="artifact-owner", password="unused"))
                db.add(AgentConfig(id=11, owner_id=1, name="审查助手", default_model_id=1))
                db.add(AgentSession(id="artifact-session", owner_id=1, agent_id=11, thread_id=thread_id, title="产物错误恢复"))
                confirmed = await submit(db, "项目代号青山-42。\n" + "一般施工说明。\n" * 600)
            confirmed = await execute_text_run(
                sessions,
                confirmed.id,
                SkillModel(responses=[AIMessage(content="上一轮审查已完成。")]),
                checkpoint,
                lease_seconds=10,
                poll_seconds=0.05,
            )
            assert confirmed.state == RunState.COMPLETED
            async with sessions.begin() as db:
                db.add(
                    Artifact(
                        id=artifact_id,
                        owner_id=1,
                        run_id=confirmed.id,
                        filename="confirmed.docx",
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        byte_size=29910,
                        sha256="a" * 64,
                        storage_key=str(uuid4()),
                        source=ArtifactOrigin.GENERATED,
                        state=ArtifactState.READY,
                    )
                )
                failed = await submit(db, "请读取错误文件并继续")
            monkeypatch.setattr(RuntimeRunConfig, "summarization_limits", lambda self: (800, 80))
            failed = await execute_text_run(
                sessions,
                failed.id,
                ArtifactTimeoutModel(responses=[AIMessage(content="unused")]),
                checkpoint,
                resolve_tools=resolver,
                lease_seconds=10,
                poll_seconds=0.05,
            )
            assert failed.state == RunState.TIMED_OUT and failed.failure_code == RunTimeoutReason.PROVIDER
            assert failed.tool_call_count == 2
            assert observations == ["history", "artifact", "provider_timeout"]
            async with sessions.begin() as db:
                rejected = await submit(db, "此前被检查点阻断的提问")
                rejected.active_thread_id = None
                rejected.state = RunState.FAILED
                rejected.failure_code = "checkpoint_mismatch"
            monkeypatch.setattr(RuntimeRunConfig, "summarization_limits", lambda self: (100000, 10000))
            for text in ("你好", "继续问候"):
                async with sessions.begin() as db:
                    followup = await submit(db, text)
                recovered = await execute_text_run(
                    sessions,
                    followup.id,
                    FollowupModel(responses=[AIMessage(content="unused")]),
                    checkpoint,
                    resolve_tools=resolver,
                    lease_seconds=10,
                    poll_seconds=0.05,
                )
                assert recovered.state == RunState.COMPLETED, recovered.failure_code
                assert recovered.model_call_count == 1 and recovered.tool_call_count == 0
            async with sessions() as db:
                stored_failed = await db.get(AgentRun, failed.id)
                assert stored_failed.state == RunState.TIMED_OUT and stored_failed.failure_code == RunTimeoutReason.PROVIDER
                stored_artifact = await db.get(Artifact, artifact_id)
                assert stored_artifact.state == ArtifactState.READY and stored_artifact.is_deleted == 0
                events = (await db.scalars(select(AgentRunEvent).where(AgentRunEvent.run_id == failed.id))).all()
                assert [event.tool_name for event in events if event.event_type == RunEventType.TOOL_STARTED] == ["read_file", "read_generated_artifact"]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
