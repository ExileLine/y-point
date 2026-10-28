# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : tasks.py


async def test_async_task(*args, **kwargs):
    """执行示例异步任务并记录调度结果。"""
    import os

    os.environ["yyx"] = "okc"
    print(f"test_async_task: {os.getenv('yyx')}", args, kwargs)


def test_sync_task(*args, **kwargs):
    """执行示例同步任务并记录调度结果。"""
    import os

    print(f"test_sync_task: {os.getenv('yyx')}", args, kwargs)
