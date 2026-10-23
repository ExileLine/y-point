from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.providers import router as providers_router
from app.api.v1.models import router as models_router
from app.api.v1.runs import router as runs_router
from app.api.v1.artifacts import router as artifacts_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(providers_router)
api_router.include_router(models_router)
api_router.include_router(runs_router)
api_router.include_router(artifacts_router)
