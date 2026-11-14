# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:17:57
# @Author  : yangyuexiong
# @File    : healthcheck.py

"""容器探针：API 验证协议，worker 验证当前进程的数据库心跳。"""

import asyncio
import json
import sys
from datetime import timedelta
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def check_worker() -> None:
    """核对本容器登记的 worker，而非其他容器的聚合存活状态。"""
    from app.core.config import get_config
    from app.db.session import AsyncSessionLocal, close_db
    from app.models.base import shanghai_now
    from app.models.run_worker_heartbeat import RunWorkerHeartbeat
    from app.services.worker_monitor import WORKER_STALE_SECONDS

    identifier = Path(get_config().WORKER_HEALTH_FILE).read_text(encoding="utf-8").strip()
    try:
        async with AsyncSessionLocal() as db:
            heartbeat = await db.get(RunWorkerHeartbeat, identifier)
            cutoff = shanghai_now(naive=True) - timedelta(seconds=WORKER_STALE_SECONDS)
            if heartbeat is None or heartbeat.stopped_at is not None or heartbeat.last_seen_at < cutoff:
                raise RuntimeError("当前 worker 心跳已失效")
    finally:
        await close_db()


def main() -> None:
    """以退出码向 Docker 返回探针结果，不打印私有配置或上游异常。"""
    try:
        if sys.argv[1] == "worker":
            asyncio.run(asyncio.wait_for(check_worker(), timeout=5))
        else:
            with urlopen("http://127.0.0.1:8000/api/v1/health", timeout=3) as response:
                payload = json.load(response)
                if response.status != 200 or payload.get("data", {}).get("status") != "ok":
                    raise RuntimeError("API 健康协议不匹配")
    except Exception:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
