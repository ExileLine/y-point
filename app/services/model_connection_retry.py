# -*- coding: utf-8 -*-
# @Time    : 2026-11-11 13:01:08
# @Author  : yangyuexiong
# @File    : model_connection_retry.py

"""仅恢复模型尚未返回数据时的短暂连接故障，保留预算、租约与取消边界。"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from anthropic import APIConnectionError as AnthropicConnectionError
from anthropic import APITimeoutError as AnthropicTimeoutError
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.runnables.config import ensure_config, get_async_callback_manager_for_config, patch_config, set_config_context
from loguru import logger
from openai import APIConnectionError as OpenAIConnectionError
from openai import APITimeoutError as OpenAITimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_config
from app.services.run_manager import reserve_model_call

ResultT = TypeVar("ResultT")


class ModelDataObserver(AsyncCallbackHandler):
    """只记录是否收到模型数据，不保存 Token、正文或工具参数。"""

    run_inline = True

    def __init__(self) -> None:
        """为当前一次模型尝试创建独立的数据到达标记。"""
        self.received_data = False

    async def on_llm_new_token(self, token: str, **kwargs: Any) -> None:
        """任意数据块到达后禁止重试，包括空正文的思考或工具参数块。"""
        self.received_data = True

    async def on_stream_event(self, event: Any, **kwargs: Any) -> None:
        """原生流式协议事件也视为数据到达，防止只观察传统 Token 回调漏判。"""
        self.received_data = True

    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """完整模型响应已返回时同样禁止重试。"""
        self.received_data = True


async def invoke_model_with_connection_retry(
    invoke: Callable[[], Awaitable[ResultT]],
    sessions: async_sessionmaker[AsyncSession],
    run_id: str,
    lease_token: str,
) -> ResultT:
    """在调用方原时限内有限重试无数据的 SDK 连接错误，每次尝试重新核对租约和累计预算。"""
    config = get_config()
    attempt = 0
    while True:
        async with sessions.begin() as db:
            await reserve_model_call(db, run_id, lease_token)
        observer = ModelDataObserver()
        inherited = ensure_config()
        callbacks = get_async_callback_manager_for_config(inherited).copy()
        callbacks.add_handler(observer, inherit=True)
        try:
            with set_config_context(patch_config(inherited, callbacks=callbacks)) as context:
                return await asyncio.create_task(invoke(), context=context)
        except (OpenAIConnectionError, AnthropicConnectionError) as exc:
            if isinstance(exc, (OpenAITimeoutError, AnthropicTimeoutError)) or observer.received_data or attempt >= config.AGENT_MODEL_CONNECTION_MAX_RETRIES:
                raise
            attempt += 1
            logger.warning(
                "模型响应前连接失败，准备有限重试 run_id={} retry={} max_retries={} delay_seconds={} error_type={}",
                run_id,
                attempt,
                config.AGENT_MODEL_CONNECTION_MAX_RETRIES,
                config.AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS,
                type(exc).__name__,
            )
        await asyncio.sleep(config.AGENT_MODEL_CONNECTION_RETRY_DELAY_SECONDS)
