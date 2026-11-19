# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:14:34
# @Author  : yangyuexiong
# @File    : mcp_connections.py

"""管理共享 MCP 连接，敏感配置只写入并在执行时复核。"""

from uuid import uuid4

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import ToolLeaseKind
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.mcp_connection import McpConnection
from app.schemas.mcp_connection import McpConnectionCreateIn, McpConnectionUpdateIn
from app.services.identity import audit
from app.services.mcp_connections import (
    cancel_connection_runs,
    connection_config,
    connection_output,
    credentials_for,
    owned_connection,
    stdio_profiles,
)
from app.services.mcp_tools import discover_mcp_tools
from app.services.tool_execution import ToolCapacityError, tool_execution_slot

router = APIRouter(prefix="/mcp-connections", tags=["mcp-connections"])


@router.get("", summary="查询自己的 MCP 连接")
async def list_connections(actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """只列出当前用户的连接，不返回凭据明文。"""
    items = (
        await db.scalars(select(McpConnection).where(McpConnection.owner_id == actor.id, McpConnection.is_deleted == 0).order_by(McpConnection.id.desc()))
    ).all()
    return api_response(data=[connection_output(item) for item in items], is_pop=False)


@router.get("/stdio-profiles", summary="查询允许使用的 stdio 启动配置")
async def list_profiles(actor: Admin = Depends(get_current_admin)):
    """仅返回配置名和允许填写的凭据键，不公开宿主配置及固定命令。"""
    try:
        profiles = stdio_profiles()
    except ValueError as exc:
        raise CustomException(status_code=503, custom_code=10005, detail="管理员 stdio 启动配置无效") from exc
    return api_response(
        data=[
            {
                "name": name,
                "env_keys": profile.get("env_keys", []),
            }
            for name, profile in profiles.items()
        ],
        is_pop=False,
    )


@router.post("", summary="创建可复用的 MCP 连接")
async def create_connection(body: McpConnectionCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """校验固定配置并加密凭据，发现工具通过独立操作执行。"""
    data = body.model_dump()
    try:
        config = connection_config(data)
        secret = credentials_for(data)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    item = McpConnection(
        owner_id=actor.id,
        name=body.name,
        version=1,
        config=config,
        encrypted_credentials=secret,
        enabled=body.enabled,
        share_credentials=body.share_credentials,
    )
    db.add(item)
    try:
        await db.flush()
        audit(db, "mcp_connection.created", actor_id=actor.id, target_id=item.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="同名 MCP 连接已存在") from None
    return api_response(http_code=201, code=201, data=connection_output(item), is_pop=False)


@router.put("/{connection_id}", summary="更新 MCP 连接并发布新版本")
async def update_connection(
    connection_id: int,
    body: McpConnectionUpdateIn,
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """普通更新保留旧工具快照，凭据轮换及权限收紧立即影响已有运行。"""
    item = await owned_connection(db, connection_id, actor.id, lock=True)
    if item.version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接版本已变化，请刷新后重试")
    data = body.model_dump()
    try:
        config = connection_config(data)
        secret = credentials_for(data, item)
        # 保留凭据时仍需按新配置核对环境变量白名单。
        from app.services.provider_credentials import decrypt_credentials

        _, env = decrypt_credentials(secret)
        if env and not set(env) <= set(config.get("env_keys", [])):
            raise ValueError("当前凭据不适用于新启动配置，请替换或清除凭据")
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    rotated = secret != item.encrypted_credentials
    tightened = not body.enabled or rotated
    shared_revoked = item.share_credentials and not body.share_credentials
    previous = item.version
    try:
        result = await db.execute(
            update(McpConnection)
            .where(
                McpConnection.id == item.id,
                McpConnection.version == previous,
                McpConnection.is_deleted == 0,
            )
            .values(
                name=body.name,
                version=previous + 1,
                config=config,
                encrypted_credentials=secret,
                enabled=body.enabled,
                share_credentials=body.share_credentials,
                credentials_revoked_through=previous if rotated else item.credentials_revoked_through,
            ),
            execution_options={
                "synchronize_session": False,
            },
        )
        if result.rowcount != 1:
            raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接版本已变化")
        if tightened or shared_revoked:
            await cancel_connection_runs(db, item, others_only=not tightened)
        audit(db, "mcp_connection.updated", actor_id=actor.id, target_id=item.id)
        await db.commit()
        await db.refresh(item)
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="同名 MCP 连接已存在") from None
    return api_response(data=connection_output(item), is_pop=False)


@router.delete("/{connection_id}", summary="删除 MCP 连接并撤销相关执行")
async def delete_connection(connection_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """保留历史配置用于审计，立即禁止已有工具继续使用该连接。"""
    item = await owned_connection(db, connection_id, actor.id, lock=True)
    item.is_deleted = item.id
    item.version += 1
    item.enabled = False
    item.name = f"deleted_{item.id}_{uuid4().hex}"
    await cancel_connection_runs(db, item)
    audit(db, "mcp_connection.deleted", actor_id=actor.id, target_id=item.id)
    await db.commit()
    return api_response()


@router.post("/{connection_id}/discover", summary="发现已保存 MCP 连接中的工具")
async def discover_connection(connection_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """使用保存的加密凭据发现工具，连接及调用共享容量和超时控制。"""
    item = await owned_connection(db, connection_id, actor.id)
    if not item.enabled:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接已停用")
    try:
        async with tool_execution_slot(
            async_sessionmaker(db.bind, expire_on_commit=False),
            actor.id,
            None,
            ToolLeaseKind.CALL,
            get_config().MCP_DISCOVERY_TIMEOUT_SECONDS,
        ):
            found = await discover_mcp_tools(item.config, item.encrypted_credentials)
    except ToolCapacityError as exc:
        raise CustomException(status_code=429, custom_code=10005, detail=str(exc)) from exc
    except Exception as exc:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接或工具发现失败，请检查配置与运行环境") from exc
    return api_response(data=found, is_pop=False)
