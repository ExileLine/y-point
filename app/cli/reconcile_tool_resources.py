# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 09:30:15
# @Author  : yangyuexiong
# @File    : reconcile_tool_resources.py

"""手动核对本部署过期工具资源、历史环境引用和容量告警。"""

import asyncio
import json

from app.db.session import AsyncSessionLocal, close_db
from app.services.tool_resources import prune_tool_resources


async def reconcile() -> dict:
    """完成资源核对，并在所有退出路径关闭数据库连接。"""
    try:
        return await prune_tool_resources(AsyncSessionLocal)
    finally:
        await close_db()


def main() -> None:
    """输出不包含密钥、正文和目录内容的核对结果。"""
    print(json.dumps(asyncio.run(reconcile()), ensure_ascii=False))


if __name__ == "__main__":
    main()
