from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.auth import router as auth_router
from app.api.v1.users import router as users_router
from app.api.v1.providers import router as providers_router
from app.api.v1.models import router as models_router
from app.api.v1.runs import router as runs_router
from app.api.v1.artifacts import router as artifacts_router
from app.api.v1.agents import router as agents_router
from app.api.v1.agent_sessions import router as agent_sessions_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(health_router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(providers_router)
api_router.include_router(models_router)
api_router.include_router(runs_router)
api_router.include_router(artifacts_router)
api_router.include_router(agents_router)
api_router.include_router(agent_sessions_router)
