# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 16:40:35
# @Author  : yangyuexiong
# @File    : probe_retry.py

import asyncio

import httpx


RETRYABLE_UPSTREAM_STATUSES = frozenset({502, 503, 504})
PROBE_RETRY_DELAY_SECONDS = 0.2


async def request_with_probe_retry(
    client: httpx.AsyncClient, method: str, url: httpx.URL, *, headers: dict[str, str], json_body: dict | None = None
) -> httpx.Response:
    """上游网关短暂故障时最多重发一次受限探测请求。"""
    for attempt in range(2):
        response = await client.request(method, url, headers=headers, json=json_body)
        if response.status_code not in RETRYABLE_UPSTREAM_STATUSES or attempt == 1:
            return response
        await asyncio.sleep(PROBE_RETRY_DELAY_SECONDS)
    raise RuntimeError("探测请求未返回结果")
