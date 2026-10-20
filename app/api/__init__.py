from fastapi import APIRouter

from app.api.v1 import api_router as api_v1_router

# 一级路由
api = APIRouter(prefix="/api", responses={404: {"description": "Not found"}})

# 二级路由
api.include_router(api_v1_router)