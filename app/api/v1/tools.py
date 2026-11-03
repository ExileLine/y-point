# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : tools.py

"""用户脚本与 MCP Tool 的发布、可见性和试运行接口。"""

import hashlib

from jsonschema import Draft202012Validator, ValidationError
from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.enums import RecoveryPolicy, ToolEffect, ToolEnvironmentState, ToolKind, ToolLeaseKind, UserRole, Visibility
from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import get_current_admin, require_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.agent_run import AgentRun
from app.models.tool import ToolInvocation, UserTool, UserToolVersion
from app.schemas.tool import McpDiscoverIn, ToolCreateIn, ToolTestIn, ToolUpdateIn, ToolEnvironmentRetryIn, ToolCredentialRevokeIn
from app.schemas.tool import ToolWriteContractVerifyIn
from app.services.identity import audit
from app.services.mcp_tools import discover_mcp_tools
from app.services.provider_credentials import decrypt_credentials
from app.services.script_sandbox import SandboxConfigurationError, run_script
from app.services.tool_environments import environment_output, initial_environment_fields, require_script_environment
from app.services.user_tools import cancel_tool_runs, model_tool_name, validate_tool_definition, version_fields
from app.services.tool_execution import ToolCapacityError, tool_execution_slot

router = APIRouter(prefix="/tools", tags=["tools"])

BUILTIN_TOOLS = (
    ("list_text_artifacts", "列出当前用户可读的文本附件"),
    ("read_text_artifact", "经审批读取当前用户的文本附件"),
    ("list_session_artifacts", "列出本会话已生成的产物"),
    ("read_generated_artifact", "读取本会话已生成的产物"),
    ("create_text_artifact", "生成文本文件"),
    ("create_document_artifact", "生成 DOCX 或 PDF"),
    ("create_spreadsheet_artifact", "生成 XLSX"),
)


async def _version(db: AsyncSession, tool: UserTool) -> UserToolVersion:
    """读取用户 Tool 当前不可变版本。"""
    return await db.scalar(
        select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == tool.current_version),
    )


def _output(tool: UserTool, version: UserToolVersion | None = None) -> dict:
    """向他人隐藏脚本源码、MCP 连接地址和凭据。"""
    data = {
        "id": tool.id,
        "owner_id": tool.owner_id,
        "name": tool.name,
        "model_name": model_tool_name(tool),
        "description": tool.description,
        "kind": tool.kind,
        "visibility": tool.visibility,
        "enabled": tool.enabled,
        "current_version": tool.current_version,
        "credentials_revoked_through": tool.credentials_revoked_through,
    }
    if version is not None:
        data.update(
            {
                "input_schema": version.input_schema,
                "effect": version.effect,
                "recovery_policy": version.recovery_policy,
                "write_contract_verified": version.write_contract_verified,
                "idempotency_key_field": version.idempotency_key_field,
                "requires_approval": version.requires_approval,
                "script_language": version.script_language,
                "source_code": version.source_code,
                "mcp_url": version.mcp_url,
                "mcp_tool_name": version.mcp_tool_name,
                "has_credentials": bool(version.encrypted_credentials),
                "share_credentials": version.share_credentials,
                "content_hash": version.content_hash,
                "verified": version.verified,
                "requirements": version.requirements,
                "environment": environment_output(version),
            }
        )
    return data


async def _owned(db: AsyncSession, tool_id: int, actor_id: int, lock: bool = False) -> UserTool:
    """查找由当前用户创建且尚未删除的 Tool。"""
    statement = select(UserTool).where(UserTool.id == tool_id, UserTool.owner_id == actor_id, UserTool.is_deleted == 0)
    tool = await db.scalar(statement.with_for_update() if lock else statement)
    if tool is None:
        raise CustomException(status_code=404, custom_code=10002, detail="Tool 不存在")
    return tool


async def _prepare(kind: str, name: str, fields: dict, token: str | None, db: AsyncSession, owner_id: int) -> tuple[dict, str, bool, str]:
    """验证 Tool 执行定义，MCP 输入结构从远端发现结果取得。"""
    if kind == ToolKind.MCP:
        try:
            sessions = async_sessionmaker(db.bind, expire_on_commit=False)
            async with tool_execution_slot(sessions, owner_id, None, ToolLeaseKind.CALL, 15):
                found = await discover_mcp_tools(fields["mcp_url"], token)
        except ToolCapacityError as exc:
            raise CustomException(status_code=429, custom_code=10005, detail=str(exc)) from exc
        except Exception as exc:
            raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接或工具发现失败") from exc
        remote = next((item for item in found if item["name"] == fields["mcp_tool_name"]), None)
        if remote is None:
            raise CustomException(status_code=409, custom_code=10005, detail="MCP 工具不存在")
        fields["input_schema"] = remote["input_schema"]
        if fields.get("effect") == ToolEffect.READ and remote.get("read_only") is not True:
            owner = await db.get(Admin, owner_id)
            if owner is None or owner.role != UserRole.ADMIN or remote.get("read_only") is False:
                raise CustomException(status_code=400, custom_code=10005, detail="远端未声明只读时须由管理员确认；显式写声明不能降为只读")
        if fields.get("effect") is None:
            fields["effect"] = ToolEffect.READ if remote.get("read_only") else ToolEffect.WRITE
    try:
        effect, _, approval, digest = validate_tool_definition(kind, name, fields)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    fields.pop("visibility")
    fields.pop("effect", None)
    return fields, effect, approval, digest


@router.get("/catalog", summary="列出当前用户可选 Tool")
async def list_tool_catalog(actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """内置 Tool 对所有用户可见，用户 Tool 按私有或公开策略筛选。"""
    items = (
        await db.scalars(
            select(UserTool)
            .where(
                UserTool.is_deleted == 0,
                or_(UserTool.owner_id == actor.id, (UserTool.visibility == Visibility.PUBLIC) & UserTool.enabled.is_(True)),
            )
            .order_by(UserTool.id.desc())
        )
    ).all()
    builtins = [
        {
            "model_name": name,
            "description": description,
            "kind": "builtin",
            "visibility": "builtin",
        }
        for name, description in BUILTIN_TOOLS
    ]
    return api_response(
        data={
            "builtin": builtins,
            "user_tools": [_output(item) for item in items],
        },
        datetime_format=False,
    )


@router.post("/mcp/discover", summary="发现 MCP 连接的工具")
async def discover_tools(body: McpDiscoverIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """校验公网连接并列出远端工具，不保存提交的凭据。"""
    try:
        sessions = async_sessionmaker(db.bind, expire_on_commit=False)
        async with tool_execution_slot(sessions, actor.id, None, ToolLeaseKind.CALL, 15):
            found = await discover_mcp_tools(body.url, body.bearer_token)
    except ToolCapacityError as exc:
        raise CustomException(status_code=429, custom_code=10005, detail=str(exc)) from exc
    except Exception as exc:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP 连接或工具发现失败") from exc
    return api_response(data=found, is_pop=False)


@router.get("/runs/{run_id}/calls", summary="查询本轮 Tool 调用记录")
async def list_tool_calls(run_id: str, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """仅向 Run 所有者返回脱敏的用户 Tool 调用状态。"""
    run = await db.get(AgentRun, run_id)
    if run is None or run.owner_id != actor.id:
        raise CustomException(status_code=404, custom_code=10002, detail="运行不存在")
    calls = (await db.scalars(select(ToolInvocation).where(ToolInvocation.run_id == run_id).order_by(ToolInvocation.id))).all()
    return api_response(
        data=[
            {
                "call_id": item.call_id,
                "tool_id": item.tool_id,
                "tool_version": item.tool_version,
                "state": item.state,
                "failure_code": item.failure_code,
                "create_time": item.create_time.isoformat(),
                "update_time": item.update_time.isoformat(),
            }
            for item in calls
        ],
        is_pop=False,
    )


@router.post("", summary="创建 Tool")
async def create_tool(body: ToolCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """脚本先私有试运行；MCP 发现成功后可直接发布。"""
    if body.kind == ToolKind.SCRIPT and body.visibility != Visibility.PRIVATE:
        raise CustomException(status_code=409, custom_code=10005, detail="脚本需先私有创建并试运行")
    try:
        fields = version_fields(body.model_dump(), None)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    fields, effect, approval, digest = await _prepare(body.kind, body.name, fields, body.mcp_bearer_token, db, actor.id)
    tool = UserTool(
        owner_id=actor.id,
        name=body.name,
        description=body.description,
        kind=body.kind,
        visibility=body.visibility,
        enabled=body.kind == ToolKind.MCP,
        current_version=1,
        is_deleted=0,
        status=1,
    )
    db.add(tool)
    try:
        await db.flush()
        db.add(
            UserToolVersion(
                tool_id=tool.id,
                version=1,
                effect=effect,
                requires_approval=approval,
                verified=body.kind == ToolKind.MCP,
                content_hash=digest,
                is_deleted=0,
                status=1,
                **initial_environment_fields(fields["requirements"]),
                **fields,
            )
        )
        audit(db, "tool.created", actor_id=actor.id, target_id=tool.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="同名 Tool 已存在") from None
    return api_response(http_code=201, code=201, data=_output(tool, await _version(db, tool)), datetime_format=False)


@router.get("/{tool_id}", summary="查询 Tool 详情")
async def get_tool(tool_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """公开 Tool 对所有用户可见，但源码和连接配置仅返回创建者。"""
    tool = await db.get(UserTool, tool_id)
    if tool is None or tool.is_deleted != 0 or tool.owner_id != actor.id and (tool.visibility != Visibility.PUBLIC or not tool.enabled):
        raise CustomException(status_code=404, custom_code=10002, detail="Tool 不存在")
    return api_response(data=_output(tool, await _version(db, tool) if tool.owner_id == actor.id else None), datetime_format=False)


@router.put("/{tool_id}", summary="更新 Tool 并发布新版本")
async def update_tool(
    tool_id: int, body: ToolUpdateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """使用期望版本保护并发编辑，旧版 Agent 关联保持固定。"""
    tool = await _owned(db, tool_id, actor.id, lock=True)
    if tool.current_version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="Tool 版本已变化")
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if any(value is None for value in changes.values()):
        raise CustomException(status_code=400, custom_code=10005, detail="更新字段不能为空")
    previous = await _version(db, tool)
    visibility = changes.get("visibility", tool.visibility)
    try:
        fields = version_fields(
            {
                **changes,
                "visibility": visibility,
            },
            previous,
        )
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    if tool.kind != ToolKind.MCP and (changes.get("clear_mcp_credentials") or changes.get("mcp_bearer_token")):
        raise CustomException(status_code=400, custom_code=10005, detail="脚本 Tool 不能管理 MCP 凭据")
    if (
        previous.encrypted_credentials
        and previous.version <= (tool.credentials_revoked_through or 0)
        and not (changes.get("mcp_bearer_token") or changes.get("clear_mcp_credentials"))
    ):
        raise CustomException(status_code=409, custom_code=10005, detail="凭据已撤销，请替换或清除后发布新版本")
    token, _ = decrypt_credentials(fields["encrypted_credentials"])
    fields, effect, approval, digest = await _prepare(tool.kind, tool.name, fields, token, db, actor.id)
    credentials_changed = bool(changes.get("mcp_bearer_token") or changes.get("clear_mcp_credentials"))
    if credentials_changed:
        tool.credentials_revoked_through = tool.current_version
        await cancel_tool_runs(db, tool, credentials_only=True)
        audit(db, "tool.credentials_rotated", actor_id=actor.id, target_id=tool.id)
    if (tool.visibility == Visibility.PUBLIC and visibility == Visibility.PRIVATE) or (
        previous.encrypted_credentials and previous.share_credentials and not fields["share_credentials"]
    ):
        await cancel_tool_runs(db, tool, others_only=True)
    if changes.get("enabled") is False:
        await cancel_tool_runs(db, tool)
    execution_changed = any(
        fields[key] != getattr(previous, key)
        for key in ("requirements", "input_schema", "script_language", "source_code", "mcp_url", "mcp_tool_name", "encrypted_credentials")
    )
    verified = tool.kind == ToolKind.MCP or previous.verified and not execution_changed
    if tool.kind == ToolKind.SCRIPT and not verified:
        visibility = Visibility.PRIVATE
    if "description" in changes:
        tool.description = changes["description"]
    tool.visibility = visibility
    tool.enabled = changes.get("enabled", tool.enabled) if verified else False
    tool.current_version += 1
    db.add(
        UserToolVersion(
            tool_id=tool.id,
            version=tool.current_version,
            effect=effect,
            requires_approval=approval,
            verified=verified,
            content_hash=digest,
            is_deleted=0,
            status=1,
            **initial_environment_fields(fields["requirements"], previous),
            **fields,
        )
    )
    audit(db, "tool.updated", actor_id=actor.id, target_id=tool.id)
    await db.commit()
    return api_response(data=_output(tool, await _version(db, tool)), datetime_format=False)


@router.delete("/{tool_id}", summary="删除我的 Tool")
async def delete_tool(tool_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """逻辑删除并立即禁止新的 Tool 调用。"""
    tool = await _owned(db, tool_id, actor.id, lock=True)
    await cancel_tool_runs(db, tool)
    tool.is_deleted = 1
    tool.enabled = False
    tool.name = f"_deleted_{tool.id}"
    audit(db, "tool.deleted", actor_id=actor.id, target_id=tool.id)
    await db.commit()
    return api_response(message="Tool 已删除")


@router.post("/{tool_id}/test", summary="在隔离环境中试运行我的 Tool")
async def test_tool(tool_id: int, body: ToolTestIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """仅允许创建者试运行；MCP 工具按正式调用策略审批，不提供直接试运行。"""
    tool = await _owned(db, tool_id, actor.id)
    version = await _version(db, tool)
    if tool.kind != ToolKind.SCRIPT:
        raise CustomException(status_code=409, custom_code=10005, detail="MCP Tool 请在 Agent 对话中审批调用")
    if version.requirements and version.environment_state != ToolEnvironmentState.READY:
        raise CustomException(status_code=409, custom_code=10005, detail="依赖环境尚未准备成功，请查看进度或重试")
    try:
        Draft202012Validator(version.input_schema).validate(body.arguments)
        environment = await require_script_environment(version)
        options = (
            {
                "environment": environment,
            }
            if environment is not None
            else {}
        )
        sessions = async_sessionmaker(db.bind, expire_on_commit=False)
        async with tool_execution_slot(sessions, actor.id, None, ToolLeaseKind.CALL, 10):
            result = await run_script(version.script_language, version.source_code, body.arguments, 10, **options)
    except ToolCapacityError as exc:
        raise CustomException(status_code=429, custom_code=10005, detail=str(exc)) from exc
    except SandboxConfigurationError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    except (ValidationError, ValueError, OSError, TimeoutError) as exc:
        raise CustomException(status_code=400, custom_code=10005, detail="脚本试运行失败或参数无效") from exc
    tool = await _owned(db, tool_id, actor.id, lock=True)
    await db.refresh(tool)
    if tool.current_version != version.version:
        raise CustomException(status_code=409, custom_code=10005, detail="Tool 版本已变化，请重新试运行")
    version.verified = True
    tool.enabled = True
    audit(db, "tool.verified", actor_id=actor.id, target_id=tool.id)
    await db.commit()
    return api_response(
        data={
            "result": result,
        },
        datetime_format=False,
    )


@router.post("/{tool_id}/environment/retry", summary="重试或重新准备我的 Tool 依赖环境")
async def retry_tool_environment(
    tool_id: int, body: ToolEnvironmentRetryIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """失败版本重新排队；已就绪环境通过新版本重建，保留旧版依赖锁。"""
    tool = await _owned(db, tool_id, actor.id, lock=True)
    if tool.current_version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="Tool 版本已变化")
    version = await _version(db, tool)
    if version.environment_state == ToolEnvironmentState.READY:
        fields = version_fields(
            {
                "visibility": Visibility.PRIVATE,
            },
            version,
        )
        fields, effect, approval, digest = await _prepare(tool.kind, tool.name, fields, None, db, actor.id)
        tool.current_version += 1
        tool.visibility = Visibility.PRIVATE
        tool.enabled = False
        version = UserToolVersion(
            tool_id=tool.id,
            version=tool.current_version,
            effect=effect,
            requires_approval=approval,
            verified=False,
            content_hash=digest,
            is_deleted=0,
            status=1,
            **initial_environment_fields(fields["requirements"]),
            **fields,
        )
        db.add(version)
    elif version.environment_state == ToolEnvironmentState.FAILED:
        version.environment_state = ToolEnvironmentState.PENDING
        version.environment_error_code = None
        version.environment_error_detail = None
    else:
        raise CustomException(status_code=409, custom_code=10005, detail="依赖环境无需准备或正在准备，请稍后查看")
    audit(db, "tool.environment_retried", actor_id=actor.id, target_id=tool.id)
    await db.commit()
    return api_response(data=_output(tool, version), datetime_format=False)


@router.post("/{tool_id}/credentials/revoke", summary="撤销我的 MCP Tool 历史凭据")
async def revoke_tool_credentials(
    tool_id: int, body: ToolCredentialRevokeIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """不访问远端即可禁用凭据并取消关联运行，恢复须显式替换或清除密钥。"""
    tool = await _owned(db, tool_id, actor.id, lock=True)
    if tool.kind != ToolKind.MCP:
        raise CustomException(status_code=400, custom_code=10005, detail="仅 MCP Tool 支持凭据撤销")
    if tool.current_version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="Tool 版本已变化")
    tool.credentials_revoked_through = tool.current_version
    tool.enabled = False
    cancelled = await cancel_tool_runs(db, tool, credentials_only=True)
    audit(db, "tool.credentials_revoked", actor_id=actor.id, target_id=tool.id)
    await db.commit()
    return api_response(
        data={
            "tool_id": tool.id,
            "credentials_revoked_through": tool.credentials_revoked_through,
            "cancelled_runs": cancelled,
        },
        datetime_format=False,
    )


@router.post("/{tool_id}/write-contract/verify", summary="验收指定版本的外部写恢复契约")
async def verify_tool_write_contract(
    tool_id: int, body: ToolWriteContractVerifyIn, actor: Admin = Depends(require_admin), db: AsyncSession = Depends(get_db_session)
):
    """管理员核对重复写入与返回丢失证据后，允许该固定版本执行幂等写操作。"""
    tool = await db.scalar(select(UserTool).where(UserTool.id == tool_id, UserTool.is_deleted == 0).with_for_update())
    if tool is None:
        raise CustomException(status_code=404, custom_code=10002, detail="Tool 不存在")
    if tool.current_version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="Tool 版本已变化")
    version = await _version(db, tool)
    if (
        tool.kind != ToolKind.MCP
        or version.effect != ToolEffect.WRITE
        or (version.recovery_policy != RecoveryPolicy.IDEMPOTENT or not version.idempotency_key_field)
    ):
        raise CustomException(status_code=400, custom_code=10005, detail="当前版本未声明完整的外部写幂等契约")
    version.write_contract_verified = True
    version.write_contract_evidence_hash = hashlib.sha256(body.evidence_reference.encode()).hexdigest()
    audit(db, "tool.write_contract_verified", actor_id=actor.id, target_id=tool.id)
    await db.commit()
    return api_response(
        data={
            "tool_id": tool.id,
            "version": version.version,
            "write_contract_verified": True,
        },
        datetime_format=False,
    )
