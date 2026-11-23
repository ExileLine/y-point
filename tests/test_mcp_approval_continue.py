# -*- coding: utf-8 -*-
# @Time    : 2026-11-23 22:00:00
# @Author  : yangyuexiong
# @File    : test_mcp_approval_continue.py

"""通过真实图和持久化审批验证逐项拒绝继续、停止、幂等和绕过防护。"""

import asyncio
from datetime import timedelta
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.agent_run_message import AgentRunMessage
from app.models.base import Base, shanghai_now
from app.services.run_dispatcher import dispatch_next_run
from app.services.run_executor import execute_text_run
from app.services.run_manager import RunConflictError, create_run, decide_approval, get_owned_approval
from app.services.run_approval_policy import approval_preview


class Model(FakeMessagesListChatModel):
    """保留图、HITL 与工具调用，只替换模型上游响应。"""

    def bind_tools(self, tools, **kwargs):
        """接受正式运行时绑定。"""
        return self


def test_preview_omits_credentials_text_and_url_query():
    """可理解目标和数量，但不披露自由文本、密钥、路径及 URL 用户信息。"""
    preview = approval_preview(
        {
            "order_id": "order-7",
            "order_ids": ["a", "b"],
            "quantity": 2,
            "url": "https://name:private-secret@example.com/private-path?token=private-secret",
            "text": "private-secret",
            "password": "private-secret",
            "path": "/private-secret",
        }
    )
    assert preview == {
        "order_id": "order-7",
        "order_ids_count": 2,
        "quantity": 2,
        "target_host": "example.com",
    }
    assert "private" not in str(preview)


@pytest.mark.parametrize("decision", ["resolve", "reject_continue", "stop", "reject"])
def test_real_graph_approval_continuation_and_legacy_stop(tmp_path: Path, decision):
    """混合批准只执行指定项；拒绝后新 ID 不绕过；旧 reject 仍停止整轮。"""
    executed = []

    @tool
    async def query_orders(order_id: str) -> str:
        """查询订单，不修改订单。"""
        executed.append(("query", order_id))
        return "已查到订单，共 2 条"

    @tool
    async def delete_orders(order_id: str) -> str:
        """删除目标订单。"""
        executed.append(("delete", order_id))
        return "已删除"

    async def tools(db, run):
        """两个动作都必须得到独立的审批决定。"""
        return [query_orders, delete_orders], {
            "query_orders": True,
            "delete_orders": True,
        }

    async def exercise():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'approval.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        checkpoint = tmp_path / "checkpoint.db"
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                db.add(Admin(id=1, username="approval-user", password="unused", status=1, is_deleted=0))
                run = await create_run(db, 1, input_text="查询订单并删除异常订单", max_model_calls=4)
            paused = await execute_text_run(
                sessions,
                run.id,
                Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "query_orders",
                                    "args": {
                                        "order_id": "order-7",
                                    },
                                    "id": "query-1",
                                },
                                {
                                    "name": "delete_orders",
                                    "args": {
                                        "order_id": "order-7",
                                    },
                                    "id": "delete-1",
                                },
                            ],
                        )
                    ]
                ),
                checkpoint,
                resolve_tools=tools,
            )
            assert paused.state == "waiting_for_approval" and not executed
            async with sessions.begin() as db:
                approval = await get_owned_approval(db, run.id, 1)
                assert [action["call_id"] for action in approval.actions] == ["query-1", "delete-1"]
                assert len({action["action_id"] for action in approval.actions}) == 2
                assert approval.actions[1]["preview"] == {
                    "order_id": "order-7",
                }
                decisions = (
                    [
                        {
                            "action_id": action["action_id"],
                            "decision": "approve" if index == 0 else "reject",
                        }
                        for index, action in enumerate(approval.actions)
                    ]
                    if decision == "resolve"
                    else None
                )
                await decide_approval(db, run.id, 1, decision, expected_approval_id=approval.id, decisions=decisions)
                # 精确同决定是幂等重试，相反决定拒绝覆盖。
                again = await decide_approval(db, run.id, 1, decision, expected_approval_id=approval.id, decisions=decisions)
                assert again.decision == decision
                with pytest.raises(RunConflictError):
                    await decide_approval(db, run.id, 1, "approve", expected_approval_id=approval.id)
            if decision in {"stop", "reject"}:
                async with sessions() as db:
                    assert (await db.get(AgentRun, run.id)).state == "cancelled"
                assert not executed
                return
            # 模型用新的调用 ID、改参数再次请求删除，正式执行层必须挡住。
            resumed_model = Model(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "delete_orders",
                                "args": {
                                    "order_id": "another-order",
                                },
                                "id": "delete-2",
                            }
                        ],
                    ),
                    AIMessage(content="订单查询汇总；删除被拒绝。"),
                ]
            )

            async def resolve_model(db, current):
                """重新调度后使用同一审批检查点。"""
                return resumed_model

            completed = await dispatch_next_run(sessions, checkpoint, resolve_model=resolve_model, resolve_tools=tools)
            assert completed.state == "completed", completed.failure_code
            assert executed == ([("query", "order-7")] if decision == "resolve" else [])
            async with sessions() as db:
                final = await db.scalar(select(AgentRunMessage).where(AgentRunMessage.run_id == run.id, AgentRunMessage.role == "assistant"))
                assert "已拒绝且未执行" in final.content and "拒绝不会回滚" in final.content
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_batch_stop_and_invalid_binding_execute_nothing(tmp_path):
    """整批出现 stop 优先终止；错序、缺项、过期不能变成隐式批准。"""

    async def exercise():
        from app.services.run_manager import claim_run, create_approval_request

        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batch.db'}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions.begin() as db:
                run = await create_run(db, 1, input_text="两个动作")
                run = await claim_run(db, run.id)
                approval = await create_approval_request(
                    db,
                    run.id,
                    run.lease_token,
                    "checkpoint",
                    [
                        {
                            "name": "one",
                            "arguments_hash": "a" * 64,
                            "action_id": "first",
                        },
                        {
                            "name": "two",
                            "arguments_hash": "b" * 64,
                            "action_id": "second",
                        },
                    ],
                )
                with pytest.raises(ValueError):
                    await decide_approval(
                        db,
                        run.id,
                        1,
                        "resolve",
                        decisions=[
                            {
                                "action_id": "second",
                                "decision": "approve",
                            }
                        ],
                    )
                chosen = [
                    {
                        "action_id": "first",
                        "decision": "approve",
                    },
                    {
                        "action_id": "second",
                        "decision": "stop",
                    },
                ]
                await decide_approval(db, run.id, 1, "resolve", decisions=chosen)
                assert run.state == "cancelled" and approval.state == "rejected"
                other = await create_run(db, 1, input_text="审批过期")
                other = await claim_run(db, other.id)
                expired = await create_approval_request(
                    db,
                    other.id,
                    other.lease_token,
                    "expired",
                    [
                        {
                            "name": "one",
                            "arguments_hash": "a" * 64,
                        }
                    ],
                )
                expired.expires_at = shanghai_now(naive=True) - timedelta(seconds=1)
                await db.flush()
                with pytest.raises(RunConflictError):
                    await decide_approval(db, other.id, 1, "reject_continue")
        finally:
            await engine.dispose()

    asyncio.run(exercise())
