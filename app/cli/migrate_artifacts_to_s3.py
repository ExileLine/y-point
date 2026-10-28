# -*- coding: utf-8 -*-
# @Time    : 2026-10-25 13:40:00
# @Author  : yangyuexiong
# @File    : migrate_artifacts_to_s3.py

"""在切换文件后端前复制并验证仍有效的本地文件。"""

import argparse
import asyncio

from app.db.session import AsyncSessionLocal, close_db
from app.services.artifact_migration import migrate_local_artifacts_to_s3
from app.services.artifact_store import artifact_root


async def _run(batch_size: int) -> None:
    """执行本地有效文件到 S3 的验证式迁移并输出统计。"""
    try:
        uploaded, verified = await migrate_local_artifacts_to_s3(AsyncSessionLocal, artifact_root(), batch_size=batch_size)
        print(f"上传 {uploaded} 个文件；验证已有远端文件 {verified} 个。")
    finally:
        await close_db()


def main() -> None:
    """解析命令行参数并启动文件迁移。"""
    parser = argparse.ArgumentParser(description="验证并复制当前业务库中有效的本地文件至 S3")
    parser.add_argument("--batch-size", type=int, default=100, help="每批读取的文件元数据数量，1 至 1000")
    args = parser.parse_args()
    asyncio.run(_run(args.batch_size))


if __name__ == "__main__":
    main()
