# -*- coding: utf-8 -*-
# @Time    : 2026-11-10 10:17:39
# @Author  : yangyuexiong
# @File    : test_script_feedback.py

"""验证脚本主动拒绝参数后，模型可在同轮修正且不丢弃审核发现项。"""

import asyncio
import json
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.enums import RunEventType, RunState, ScriptLanguage, ToolEffect, ToolInvocationState, ToolKind, UserRole, Visibility
from app.models.admin import Admin
from app.models.agent_config import AgentConfig
from app.models.agent_run_event import AgentRunEvent
from app.models.base import Base
from app.models.tool import AgentTool, ToolInvocation, UserTool, UserToolVersion
from app.services import worker_tools
from app.services.run_executor import execute_text_run
from app.services.run_manager import create_run
from app.services.script_sandbox import run_script
from app.services.user_tools import snapshot_run_tools

SOURCE = """import json, sys
payload = json.load(sys.stdin)
if payload['unchecked_scope'] not in payload['summary']:
    print('部分审查须在 summary 中逐字写明 unchecked_scope', file=sys.stderr)
    sys.exit(1)
print(json.dumps({'issue_count': len(payload['findings'])}))
"""


class ScriptRepairModel(FakeMessagesListChatModel):
    """确认正式 ToolMessage 带有业务修正提示。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """保留假模型响应，由正式 worker 工具执行脚本及参数纠正。"""
        return self

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        """检查脚本主动拒绝输入的受控反馈后返回下一条预设响应。"""
        errors = [message for message in messages if isinstance(message, ToolMessage) and message.status == "error"]
        if errors:
            feedback = json.loads(errors[-1].content)
            assert feedback["error"] == "script_input_rejected"
            assert feedback["retryable"] is True
            assert "summary" in feedback["message"] and "unchecked_scope" in feedback["message"]
        return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)


@pytest.mark.parametrize("effect", [ToolEffect.READ, ToolEffect.WRITE])
def test_script_business_validation_can_be_repaired(tmp_path: Path, effect: ToolEffect) -> None:
    """使用真实 Docker、worker、图和调用审计复现跨字段校验错误后的修正。"""

    async def exercise():
        """使用隔离数据库及真实沙箱核对只读纠正与写工具禁止重放。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'feedback.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="script-feedback-user", password="unused", role=UserRole.USER))
                db.add(AgentConfig(id=1, owner_id=1, name="脚本反馈测试", default_model_id=1))
                db.add(
                    UserTool(
                        id=1,
                        owner_id=1,
                        name="check_report",
                        description="校验报告",
                        kind=ToolKind.SCRIPT,
                        visibility=Visibility.PRIVATE,
                        enabled=True,
                        current_version=1,
                    )
                )
                db.add(
                    UserToolVersion(
                        tool_id=1,
                        version=1,
                        content_hash="a" * 64,
                        effect=effect,
                        requires_approval=False,
                        verified=True,
                        script_language=ScriptLanguage.PYTHON,
                        source_code=SOURCE,
                        input_schema={
                            "type": "object",
                            "properties": {
                                "summary": {
                                    "type": "string",
                                },
                                "unchecked_scope": {
                                    "type": "string",
                                },
                                "findings": {
                                    "type": "array",
                                },
                            },
                            "required": ["summary", "unchecked_scope", "findings"],
                        },
                    )
                )
                db.add(AgentTool(agent_id=1, tool_id=1, tool_version=1))
                await db.flush()
                run = await create_run(db, 1, agent_id=1, input_text="审查方案", max_model_calls=3, max_tool_calls=2)
                await snapshot_run_tools(db, run.id, 1, 1)
            invalid = {
                "summary": "本次仅核查部分内容",
                "unchecked_scope": "附件计算书",
                "findings": [
                    {
                        "problem": "示例问题",
                    }
                ],
            }
            repaired = {
                **invalid,
                "summary": "本次未核查附件计算书",
            }
            model = ScriptRepairModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "check_report",
                                "args": invalid,
                                "id": "scope-mismatch",
                            }
                        ],
                    ),
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "check_report",
                                "args": repaired,
                                "id": "scope-repaired",
                            }
                        ],
                    ),
                    AIMessage(content="报告校验完成"),
                ]
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
            assert result.state == (RunState.COMPLETED if effect == ToolEffect.READ else RunState.FAILED), result.failure_code
            assert result.tool_call_count == (2 if effect == ToolEffect.READ else 1)
            assert result.model_call_count == (3 if effect == ToolEffect.READ else 1)
            assert invalid["findings"] == repaired["findings"]
            async with sessions() as db:
                invocations = list(
                    await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run.id).order_by(ToolInvocation.id))
                )
                expected = (
                    [(ToolInvocationState.FAILED, "execution_error"), (ToolInvocationState.COMPLETED, None)]
                    if effect == ToolEffect.READ
                    else [(ToolInvocationState.UNKNOWN, "execution_interrupted")]
                )
                assert [(item.state, item.failure_code) for item in invocations] == expected
                events = list(await db.scalars(select(AgentRunEvent.event_type).where(AgentRunEvent.run_id == run.id)))
                assert events.count(RunEventType.TOOL_FAILED) == 1
                assert events.count(RunEventType.TOOL_COMPLETED) == (1 if effect == ToolEffect.READ else 0)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "source",
    [
        "raise RuntimeError('PRIVATE-TRACEBACK')",
        "import sys; print('line1\\nline2', file=sys.stderr); sys.exit(1)",
        "import sys; print('x' * 513, file=sys.stderr); sys.exit(1)",
        "import sys; print('{}'); print('rejection', file=sys.stderr); sys.exit(1)",
        "import sys; print('rejection', file=sys.stderr); sys.exit(2)",
        "import sys; sys.stderr.buffer.write(b'\\xff'); sys.exit(1)",
        "import sys; sys.stderr.buffer.write(b'\\x1b[31mprivate'); sys.exit(1)",
    ],
)
def test_script_crashes_and_logs_stay_opaque(source: str) -> None:
    """堆栈、多行日志、混杂输出及其他退出码不自动成为可重试业务反馈。"""
    with pytest.raises(ValueError, match="^脚本执行失败$"):
        asyncio.run(run_script(ScriptLanguage.PYTHON, source, {}, 10))
