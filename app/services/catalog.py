# -*- coding: utf-8 -*-
# @Time    : 2026-10-22 09:46:52
# @Author  : yangyuexiong
# @File    : catalog.py

from pydantic import SecretStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.models.model_catalog import ModelCatalog
from app.models.provider_connection import ProviderConnection
from app.schemas.provider import ProviderCreateIn, ProviderUpdateIn
from app.services.provider_credentials import decrypt_credentials, encrypt_credentials
from app.services.provider_probe import validate_custom_headers


async def get_provider_or_404(db: AsyncSession, provider_id: int) -> ProviderConnection:
    """读取未删除的供应商连接。"""
    provider = await db.get(ProviderConnection, provider_id)
    if provider is None or provider.is_deleted != 0:
        raise CustomException(status_code=404, custom_code=10002, detail="供应商连接不存在")
    return provider


async def get_model_or_404(db: AsyncSession, model_id: int) -> ModelCatalog:
    """读取未删除的可选模型。"""
    model = await db.get(ModelCatalog, model_id)
    if model is None or model.is_deleted != 0:
        raise CustomException(status_code=404, custom_code=10002, detail="模型不存在")
    return model


async def commit_catalog(db: AsyncSession) -> None:
    """提交目录变更，将唯一约束冲突转换为业务错误。"""
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10003, detail="连接名称或模型 ID 已被使用") from None


async def flush_catalog(db: AsyncSession) -> None:
    """生成新增记录的 ID 并处理唯一约束冲突。"""
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10003, detail="连接名称或模型 ID 已被使用") from None


def set_provider_credentials(provider: ProviderConnection, body: ProviderCreateIn | ProviderUpdateIn) -> None:
    """只替换请求中明确传入的凭据，空值表示删除原凭据。"""
    fields = body.model_fields_set
    if "api_key" not in fields and "custom_headers" not in fields:
        return
    api_key, headers = decrypt_credentials(provider.encrypted_credentials)
    if "api_key" in fields:
        api_key = body.api_key.get_secret_value().strip() if isinstance(body.api_key, SecretStr) else None
    if "custom_headers" in fields:
        headers = {name: value.get_secret_value() for name, value in (body.custom_headers or {}).items()}
    validate_custom_headers(headers)
    provider.encrypted_credentials = encrypt_credentials(api_key, headers)
    provider.has_api_key = bool(api_key)
    provider.has_custom_headers = bool(headers)
