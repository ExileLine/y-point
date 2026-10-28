# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 16:53:12
# @Author  : yangyuexiong
# @File    : health.py

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.response import api_response
from app.db.session import get_db_session
from app.services.worker_monitor import worker_available

router = APIRouter(tags=["health"])


@router.get("/health", summary="检查 API 存活状态")
def health_check():
    """返回 API 进程的基础存活状态。"""
    return api_response(
        data={
            "status": "ok",
        }
    )


@router.get("/health/worker", summary="检查运行执行器存活状态")
async def worker_health(db: AsyncSession = Depends(get_db_session)):
    """以业务库中的新鲜心跳判断是否有可用执行器。"""
    available = await worker_available(db)
    if not available:
        return api_response(
            http_code=503,
            code=503,
            message="运行执行器不可用",
            data={
                "worker_available": False,
            },
        )
    return api_response(
        data={
            "worker_available": True,
        }
    )
