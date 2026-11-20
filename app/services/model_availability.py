"""维护实际请求确认的模型访问故障，独立于历史能力验证。"""

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import RunModelFailureReason
from app.models.base import shanghai_now
from app.models.model_catalog import ModelCatalog

MODEL_AVAILABILITY_MESSAGES = {
    RunModelFailureReason.NOT_FOUND: '当前供应商未找到该模型或接口（404）。请管理员核对供应商模型 ID、接口地址、部署状态及 API Key 的模型授权，修复后在“模型与供应商”测试文本调用。重建 Agent 或会话无法解决此问题。',
    RunModelFailureReason.AUTHENTICATION: '模型供应商认证失败（401）。请管理员在“模型与供应商”更新 API Key 或认证配置，再测试文本调用；重建 Agent 或会话无法解决此问题。',
    RunModelFailureReason.ACCESS_DENIED: '当前凭据无权调用该模型（403）。请管理员检查 API Key 的模型授权，修复后在“模型与供应商”测试文本调用；重建 Agent 或会话无法解决此问题。',
}


def model_availability_notice(model: ModelCatalog) -> str | None:
    """仅返回受控原因与操作指引，不暴露上游响应或凭据。"""
    return MODEL_AVAILABILITY_MESSAGES.get(model.availability_failure)


async def record_model_availability(
    db: AsyncSession,
    model: ModelCatalog | None,
    *,
    failure: str | None,
    available: bool = False,
    upstream_status_code: int | None = None,
    snapshot: tuple | None = None,
) -> None:
    """仅明确的访问拒绝阻断选模；成功重测解除，过期配置的结果不覆盖新配置。"""
    if model is None:
        return
    reason = {401: RunModelFailureReason.AUTHENTICATION, 403: RunModelFailureReason.ACCESS_DENIED, 404: RunModelFailureReason.NOT_FOUND}.get(upstream_status_code, failure)
    if not available and reason not in MODEL_AVAILABILITY_MESSAGES:
        return
    model_id, provider_id, external_id, updated = snapshot or (model.id, model.provider_id, model.external_id, model.update_time)
    await db.execute(
        update(ModelCatalog)
        .where(ModelCatalog.id == model_id, ModelCatalog.provider_id == provider_id, ModelCatalog.external_id == external_id, ModelCatalog.update_time == updated, ModelCatalog.is_deleted == 0)
        .values(availability_failure=None if available else reason, availability_checked_at=shanghai_now(naive=True))
        .execution_options(synchronize_session=False),
    )
