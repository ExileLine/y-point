# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 01:42:01
# @Author  : yangyuexiong
# @File    : test_tool_arguments.py

"""验证模型遗漏脚本必填参数时可获得脱敏反馈并在同轮修正。"""

import asyncio
import json
from pathlib import Path
from typing import ClassVar

import pytest

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run_event import AgentRunEvent
from app.models.base import Base
from app.models.tool import AgentTool, ToolInvocation, UserTool, UserToolVersion
from app.services import worker_tools
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run
from app.services.user_tools import snapshot_run_tools


def report_schema() -> dict:
    """保留真实失败的五个字段与必填约束，不复制施工文档或模型审查结论。"""
    return {
        "type": "object",
        "properties": {
            "plan_name": {
                "type": "string",
            },
            "summary": {
                "type": "string",
            },
            "review_complete": {
                "type": "boolean",
            },
            "unchecked_scope": {
                "type": "string",
            },
            "findings": {
                "type": "array",
            },
        },
        "required": ["plan_name", "summary", "review_complete", "unchecked_scope", "findings"],
        "additionalProperties": False,
    }


class ArgumentRepairModel(FakeMessagesListChatModel):
    """从工具错误中读取字段问题，再生成完整参数。"""

    errors: ClassVar[list[ToolMessage]] = []

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """允许测试模型进入正式工具编排图。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """检查反馈能指导修正，且没有泄漏原始输入值与内部异常。"""
        failures = [item for item in messages if isinstance(item, ToolMessage) and item.status == "error"]
        if failures:
            self.errors.extend(failures)
            data = json.loads(failures[-1].content)
            assert data["error"] == "invalid_tool_arguments"
            assert data["retryable"] is True
            feedback = json.dumps(data, ensure_ascii=False)
            assert "unchecked_scope" in feedback and "findings" in feedback
            assert "PRIVATE-REVIEW-TEXT" not in feedback and "ValidationError" not in feedback
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("kind", ["script", "mcp"])
@pytest.mark.parametrize("execution_fails", [False, True])
def test_missing_script_fields_can_be_repaired_in_same_run(tmp_path: Path, monkeypatch, kind: str, execution_fails: bool) -> None:
    """复现本次缺少 unchecked_scope/findings 的失败，校验修正前不启动脚本。"""

    async def exercise() -> None:
        """执行真实运行时和正式 worker 工具，仅替代最后的沙箱执行边界。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'arguments.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        executed = []
        ArgumentRepairModel.errors.clear()

        async def sandbox(language, source, arguments, timeout, **kwargs):
            """只接受完整参数，错误输入不得穿过执行边界。"""
            assert arguments["unchecked_scope"] == "" and arguments["findings"] == []
            executed.append(arguments)
            if execution_fails:
                raise RuntimeError("PRIVATE-RUNTIME-DIAGNOSTIC")
            return '{"checked": true}'

        async def remote(url, name, arguments, token, timeout, expected_schema, **kwargs):
            """仿真外部写调用边界，参数错误时不得进入，正确时保留服务端幂等键。"""
            assert arguments["request_id"]
            return await sandbox("mcp", "", arguments, timeout)

        monkeypatch.setattr(worker_tools, "run_script", sandbox)
        monkeypatch.setattr(worker_tools, "invoke_mcp_tool", remote)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="argument-user", password="unused", role="user"))
                db.add(AgentConfig(id=1, owner_id=1, name="参数修正测试", default_model_id=1))
                db.add(
                    UserTool(
                        id=1,
                        owner_id=1,
                        name="check_report",
                        description="校验报告字段",
                        kind=kind,
                        visibility="private",
                        enabled=True,
                        current_version=1,
                    ),
                )
                schema = report_schema()
                if kind == "mcp":
                    schema["properties"]["request_id"] = {
                        "type": "string",
                    }
                    schema["required"].append("request_id")
                db.add(
                    UserToolVersion(
                        tool_id=1,
                        version=1,
                        content_hash="a" * 64,
                        input_schema=schema,
                        effect="write" if kind == "mcp" else "read",
                        recovery_policy="idempotent" if kind == "mcp" else "safe",
                        idempotency_key_field="request_id" if kind == "mcp" else None,
                        write_contract_verified=kind == "mcp",
                        mcp_url="https://example.invalid/mcp" if kind == "mcp" else None,
                        mcp_tool_name="review" if kind == "mcp" else None,
                        requires_approval=False,
                        verified=True,
                        script_language="python",
                        source_code="print('{}')",
                    ),
                )
                db.add(AgentTool(agent_id=1, tool_id=1, tool_version=1))
                await db.flush()
                run = await create_run(db, 1, agent_id=1, input_text="检查参数修正", max_model_calls=3, max_tool_calls=2)
                await snapshot_run_tools(db, run.id, 1, 1)
            invalid = {
                "plan_name": "测试方案",
                "summary": "PRIVATE-REVIEW-TEXT",
                "review_complete": True,
            }
            valid = {
                **invalid,
                "unchecked_scope": "",
                "findings": [],
            }
            model = ArgumentRepairModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "check_report",
                                "args": invalid,
                                "id": "missing-fields",
                            },
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "check_report",
                                "args": valid,
                                "id": "corrected-fields",
                            },
                        ],
                    ),
                    AIMessage(content="参数已修正且校验完成"),
                ],
            )
            result = await execute_text_run(
                sessions,
                run.id,
                model,
                tmp_path / "checkpoints.db",
                resolve_tools=worker_tools.make_worker_tool_resolver(sessions),
                lease_seconds=10,
                poll_seconds=1,
            )
            if execution_fails:
                assert result.state == "failed" and result.failure_code == "model_error"
            else:
                assert result.state == "completed", result.failure_code
            assert result.tool_call_count == 2 and result.model_call_count == (2 if execution_fails else 3)
            assert len(executed) == 1 and ArgumentRepairModel.errors
            async with sessions() as db:
                invocations = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id).order_by(ToolInvocation.id))).all()
                assert [(item.state, item.failure_code) for item in invocations] == [
                    ("failed", "invalid_tool_arguments"),
                    (
                        ("unknown" if kind == "mcp" else "failed") if execution_fails else "completed",
                        "execution_interrupted" if execution_fails else None,
                    ),
                ]
                events = list(await db.scalars(select(AgentRunEvent.event_type).where(AgentRunEvent.run_id == run.id)))
                assert events.count("tool_failed") == (2 if execution_fails else 1)
                assert events.count("tool_completed") == (0 if execution_fails else 1)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_argument_feedback_is_bounded_and_contains_no_values() -> None:
    """大量嵌套类型错误只返回有限字段路径与规则，不回显私有业务值。"""
    from app.services.tool_arguments import ToolArgumentsError, validate_tool_arguments

    schema = {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "integer",
                },
            },
        },
    }
    with pytest.raises(ToolArgumentsError) as error:
        validate_tool_arguments(
            schema,
            {
                "findings": ["PRIVATE-REVIEW-TEXT" * 100] * 1000,
            },
        )
    payload = json.loads(str(error.value))
    assert payload["truncated"] is True and len(payload["issues"]) == 8
    assert all(issue["rule"] == "type" and issue["path"].startswith("/findings/") for issue in payload["issues"])
    assert "PRIVATE-REVIEW-TEXT" not in str(error.value) and len(str(error.value)) < 2048


def test_only_argument_errors_are_recoverable() -> None:
    """依赖、授权及外部执行错误不能被参数修正通道误吞或泄露给模型。"""
    from langchain_core.tools import ToolException
    from app.services.tool_arguments import is_tool_argument_error, tool_argument_error_content

    error = ToolException("PRIVATE-UPSTREAM-ERROR")
    with pytest.raises(ToolException) as caught:
        tool_argument_error_content(error)
    assert caught.value is error
    assert not is_tool_argument_error("not-json")
    assert not is_tool_argument_error('{"error":"upstream"}')
