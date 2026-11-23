# -*- coding: utf-8 -*-
# @Time    : 2026-11-19 09:14:34
# @Author  : yangyuexiong
# @File    : mcp_connections.py

"""管理共享 MCP 连接，敏感配置只写入并在执行时复核。"""

from uuid import uuid4

from fastapi import APIRouter, Depends, Query
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
from app.models.mcp_connection import McpConnection, McpConnectionShare
from app.schemas.mcp_connection import McpConnectionCreateIn, McpConnectionUpdateIn, McpConnectionShareIn, McpSyncIn
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


@router.get("/catalog", summary="查询有权使用的 MCP 连接摘要与工具子集")
async def list_connection_catalog(actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """所有者和指定接收者可见分组摘要，不公开 URL、Headers、命令或凭据。"""
    from app.services.mcp_directory import connection_directory

    return api_response(data=await connection_directory(db, actor.id), is_pop=False)


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
    data = body.model_dump(exclude_none=True, exclude_unset=True)
    data.setdefault("transport", body.transport)
    try:
        config = connection_config(data)
        secret = credentials_for(data)
        if config["transport"] == "streamable_http":
            from app.services.provider_credentials import decrypt_credentials

            _, headers = decrypt_credentials(secret)
            if headers:
                config["sensitive_header_names"] = sorted(headers)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    item = McpConnection(
        owner_id=actor.id,
        name=body.name,
        version=1,
        config=config,
        encrypted_credentials=secret,
        enabled=body.enabled,
        approval_mode=body.approval_mode,
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
    data = body.model_dump(exclude_none=True, exclude_unset=True)
    data.setdefault("transport", body.transport)
    try:
        config = connection_config(data, item)
        secret = credentials_for(data, item)
        # 保留凭据时仍需按新配置核对环境变量白名单。
        from app.services.provider_credentials import decrypt_credentials

        _, env = decrypt_credentials(secret)
        if config["transport"] == "streamable_http" and env:
            config["sensitive_header_names"] = sorted(env)
        if config["transport"] == "stdio" and env and not set(env) <= set(config.get("env_keys", [])):
            raise ValueError("当前凭据不适用于新启动配置，请替换或清除凭据")
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    rotated = secret != item.encrypted_credentials
    scope_changed = any(config.get(key) != item.config.get(key) for key in ("auth_type", "api_key_header", "headers"))
    # 旧 HTTP 配置升级为显式 none/bearer 不改变数据范围。
    old_http = item.config.get("transport") == "streamable_http"
    if old_http and "auth_type" not in item.config:
        from app.services.provider_credentials import decrypt_credentials

        old_key, _ = decrypt_credentials(item.encrypted_credentials)
        scope_changed = config.get("auth_type", "bearer" if old_key else "none") != ("bearer" if old_key else "none") or bool(config.get("headers"))
    tightened = not body.enabled or rotated or scope_changed
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
                approval_mode=body.approval_mode if "approval_mode" in body.model_fields_set else item.approval_mode,
                share_credentials=body.share_credentials,
                credentials_revoked_through=previous if rotated else item.credentials_revoked_through,
                config_revoked_through=previous if scope_changed else item.config_revoked_through,
            ),
            execution_options={
                "synchronize_session": False,
            },
        )
        if result.rowcount != 1:
            raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接版本已变化")
        if tightened or shared_revoked:
            await cancel_connection_runs(db, item, others_only=not tightened)
        await db.refresh(item)
        from app.services.mcp_policy import cancel_tightened_tool_runs

        await cancel_tightened_tool_runs(db, connection_id=item.id)
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
    return api_response(data=await _discover_saved(db, item, actor.id), is_pop=False)


async def _discover_saved(db: AsyncSession, item: McpConnection, actor_id: int) -> list[dict]:
    """差异预览和发布复核共用正式发现的凭据、容量与超时约束。"""
    if not item.enabled:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接已停用")
    try:
        async with tool_execution_slot(
            async_sessionmaker(db.bind, expire_on_commit=False), actor_id, None, ToolLeaseKind.CALL, get_config().MCP_DISCOVERY_TIMEOUT_SECONDS
        ):
            return await discover_mcp_tools(item.config, item.encrypted_credentials)
    except ToolCapacityError as exc:
        raise CustomException(status_code=429, custom_code=10005, detail=str(exc)) from exc
    except Exception as exc:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 发现失败，请检查鉴权、Headers 与运行环境") from exc


@router.post("/{connection_id}/sync-preview", summary="预览远端函数与已发布工具的差异")
async def preview_sync(connection_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """只发现和比较，不创建工具、不修改 Agent 或执行授权。"""
    from app.services.mcp_sync import connection_preview

    item = await owned_connection(db, connection_id, actor.id)
    return api_response(data=await connection_preview(db, item, await _discover_saved(db, item, actor.id)), is_pop=False)


@router.post("/{connection_id}/sync", summary="确认选择并批量发布 MCP 工具")
async def apply_sync(connection_id: int, body: "McpSyncIn", actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """重复请求返回原逐项结果；发生变化先重新预览，单项失败不回滚其他成功项。"""
    from app.core.enums import McpOperationKind
    from app.services.mcp_sync import apply_connection_sync, replay_operation

    item = await owned_connection(db, connection_id, actor.id)
    replay = await replay_operation(db, actor.id, McpOperationKind.CONNECTION_SYNC, connection_id, body)
    if replay is not None:
        return api_response(data=replay, is_pop=False)
    remote = await _discover_saved(db, item, actor.id)
    try:
        result = await apply_connection_sync(db, item, body, remote)
        await db.commit()
    except (IntegrityError, CustomException) as exc:
        await db.rollback()
        replay = await replay_operation(db, actor.id, McpOperationKind.CONNECTION_SYNC, connection_id, body)
        if replay is not None:
            return api_response(data=replay, is_pop=False)
        if isinstance(exc, CustomException):
            raise
        raise CustomException(status_code=409, custom_code=10005, detail="同步发生并发变化，请刷新预览后重试") from None
    return api_response(data=result, is_pop=False)


@router.get("/{connection_id}/shares", summary="查询连接指定用户授权")
async def list_connection_shares(connection_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """仅连接所有者可管理授权，不由 Agent 共享转授。"""
    from app.services.mcp_directory import share_output

    await owned_connection(db, connection_id, actor.id)
    rows = await db.execute(
        select(McpConnectionShare, Admin)
        .join(Admin, Admin.id == McpConnectionShare.user_id)
        .where(McpConnectionShare.connection_id == connection_id, McpConnectionShare.is_deleted == 0)
        .order_by(Admin.id)
    )
    return api_response(data=[share_output(share, user) for share, user in rows], is_pop=False)


@router.put("/{connection_id}/shares/{user_id}", summary="授予或修改指定用户的连接工具子集")
async def put_connection_share(
    connection_id: int, user_id: int, body: McpConnectionShareIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """工具子集整体替换，凭据借用独立选择；收紧授权立即取消受影响运行。"""
    from app.services.mcp_directory import save_connection_share

    try:
        data = await save_connection_share(db, connection_id, user_id, actor.id, body)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="连接授权发生并发变化，请刷新后重试") from None
    return api_response(data=data, is_pop=False)


@router.delete("/{connection_id}/shares/{user_id}", summary="撤销指定用户的连接授权")
async def delete_connection_share(
    connection_id: int,
    user_id: int,
    expected_version: int = Query(ge=1),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """校验授权版本后撤销摘要、子集及凭据借用，并取消该用户相关运行。"""
    from app.services.mcp_access import connection_share
    from app.services.mcp_directory import cancel_shared_runs

    await owned_connection(db, connection_id, actor.id, lock=True)
    share = await connection_share(db, connection_id, user_id)
    if share is None or share.version != expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="连接共享授权已变化，请刷新后重试")
    await cancel_shared_runs(db, connection_id, user_id, share.tool_ids)
    changed = await db.execute(
        update(McpConnectionShare)
        .where(McpConnectionShare.id == share.id, McpConnectionShare.version == expected_version, McpConnectionShare.is_deleted == 0)
        .values(is_deleted=share.id, status=0, version=expected_version + 1)
        .execution_options(synchronize_session=False)
    )
    if changed.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="连接共享授权已变化，请刷新后重试")
    audit(db, "mcp_connection.share_revoked", actor_id=actor.id, target_id=user_id, subject=str(connection_id))
    await db.commit()
    return api_response()
