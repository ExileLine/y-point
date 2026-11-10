# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:08:28
# @Author  : yangyuexiong
# @File    : skill_packages.py

"""Skill 文件工作区、草稿校验发布、整包往返和包内脚本试运行。"""

import asyncio
import hashlib
import io
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from urllib.parse import quote

import yaml
from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from jsonschema import Draft202012Validator, ValidationError

from app.api.v1.skills import _owned, _output
from app.core.config import get_config
from app.core.custom_exception import CustomException
from app.core.enums import ToolEnvironmentState, ToolKind, ToolLeaseKind, Visibility
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.skill import Skill, SkillVersion
from app.models.skill_package import SkillDraft, SkillScript, SkillVersionFile
from app.models.skill_template import SkillTemplate
from app.models.tool import UserTool, UserToolVersion
from app.schemas.skill_package import SkillPackageCreateIn, SkillDraftStartIn, SkillDraftUpdateIn, SkillDraftRevisionIn, SkillScriptTestIn
from app.services.artifact_store import ArtifactValidationError
from app.services.identity import audit
from app.services.script_sandbox import run_script
from app.services.skill_packages import (
    MAX_PACKAGE_BYTES,
    MAX_RESOURCE_BYTES,
    check_entries,
    check_quota,
    cleanup_resources,
    draft_texts,
    execution_config,
    package_digest,
    package_path,
    read_resource,
    replace_draft,
    script_fields,
    store_resource,
    unpack_package,
    validate_draft,
    version_entries,
)
from app.services.skill_reports import validate_fields_schema
from app.services.skills import selected_skills, validate_package
from app.services.tool_environments import initial_environment_fields, environment_output, require_script_environment
from app.services.tool_execution import tool_execution_slot, ToolCapacityError

router = APIRouter(prefix="/skills", tags=["skill-packages"])


@asynccontextmanager
async def resource_transaction(db: AsyncSession, owner_id: int):
    """统一处理草稿资源配额、事务与失败后的本地或对象存储清理。"""
    saved = []
    try:
        yield saved
        await check_quota(db, owner_id, saved)
        db.add_all([item for item, _ in saved])
        await db.commit()
    except BaseException as exc:
        await db.rollback()
        await cleanup_resources(saved)
        if isinstance(exc, (ValueError, yaml.YAMLError, zipfile.BadZipFile, ArtifactValidationError)):
            raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
        if isinstance(exc, IntegrityError):
            raise CustomException(status_code=409, custom_code=10005, detail="文件包名称或草稿版本冲突，请刷新") from None
        raise


async def _draft(db: AsyncSession, skill_id: int, owner_id: int, revision: int | None = None) -> tuple[Skill, SkillDraft]:
    """读取所有者工作区并在处理正文前检查预期修订号。"""
    item = await _owned(db, skill_id, owner_id)
    draft = await db.scalar(select(SkillDraft).where(SkillDraft.skill_id == skill_id))
    if draft is None:
        raise CustomException(status_code=404, custom_code=10002, detail="草稿不存在，请先创建工作区草稿")
    if revision is not None and draft.revision != revision:
        raise CustomException(status_code=409, custom_code=10005, detail="Skill 草稿已变化，请刷新后比较修改")
    return item, draft


def draft_output(draft: SkillDraft) -> dict:
    """仅输出逻辑清单，隐藏私有文件存储键。"""
    return {
        "revision": draft.revision,
        "base_version": draft.base_version,
        "content_hash": package_digest(draft.files),
        "files": [dict(entry, path=path) for path, entry in sorted(draft.files.items())],
        "settings": draft.settings,
    }


async def _create_package(db: AsyncSession, owner_id: int, files: dict[str, bytes]) -> Skill:
    """创建尚未发布的私有 Skill，初始资源只属于草稿。"""
    name, description, _, _ = validate_package(
        {
            "SKILL.md": files.get("SKILL.md", b"").decode("utf-8"),
        }
    )
    async with resource_transaction(db, owner_id) as saved:
        item = Skill(
            owner_id=owner_id,
            name=name,
            description=description,
            visibility=Visibility.PRIVATE,
            enabled=False,
            current_version=0,
            status=1,
            is_deleted=0,
        )
        db.add(item)
        await db.flush()
        entries = {path: await store_resource(db, owner_id, path, data, saved) for path, data in files.items()}
        check_entries(entries)
        db.add(
            SkillDraft(
                skill_id=item.id,
                revision=1,
                base_version=0,
                files=entries,
                settings={
                    "category": None,
                    "visibility": Visibility.PRIVATE,
                    "enabled": True,
                },
            )
        )
        audit(db, "skill.draft_created", actor_id=owner_id, target_id=item.id)
    return item


@router.post("/packages", summary="创建尚未发布的 Skill 文件工作区")
async def create_package(body: SkillPackageCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """使用标准入口文件创建草稿，发布前不能关联 Agent。"""
    text = (
        "---\n"
        + yaml.safe_dump(
            {
                "name": body.name,
                "description": body.description,
            },
            allow_unicode=True,
        )
        + "---\n\n# 执行步骤\n\n"
    )
    try:
        item = await _create_package(
            db,
            actor.id,
            {
                "SKILL.md": text.encode(),
            },
        )
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    return api_response(http_code=201, code=201, data=_output(item), datetime_format=False)


@router.post("/packages/import", summary="将含二进制资源的完整 ZIP 导入新 Skill 草稿")
async def import_package(
    file: UploadFile = File(description="最多 32 MiB 的单个 Skill ZIP"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """导入包含一个根目录的标准包；同名对象明确冲突，不覆盖已有草稿。"""
    try:
        files = unpack_package(await file.read(MAX_PACKAGE_BYTES + 1))
        item = await _create_package(db, actor.id, files)
    except (ValueError, zipfile.BadZipFile, yaml.YAMLError) as exc:
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    finally:
        await file.close()
    return api_response(http_code=201, code=201, data=_output(item), datetime_format=False)


@router.get("/{skill_id}/workspace", summary="读取 Skill 文件工作区及发布版本目录")
async def workspace(skill_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """所有者可读取草稿与历史；其他用户只能查看公开启用的当前包。"""
    item = (
        (await selected_skills(db, [skill_id], actor.id))[0]
        if await db.scalar(select(Skill.owner_id).where(Skill.id == skill_id)) != actor.id
        else await _owned(db, skill_id, actor.id)
    )
    versions = list(await db.scalars(select(SkillVersion).where(SkillVersion.skill_id == skill_id).order_by(SkillVersion.version.desc())))
    if item.owner_id != actor.id:
        versions = [version for version in versions if version.version == item.current_version]
    draft = await db.scalar(select(SkillDraft).where(SkillDraft.skill_id == skill_id)) if item.owner_id == actor.id else None
    current = next((version for version in versions if version.version == item.current_version), None)
    return api_response(
        data={
            "skill": _output(item, current),
            "draft": draft_output(draft) if draft else None,
            "versions": [
                {
                    "version": version.version,
                    "content_hash": version.content_hash,
                    "manifest": version.manifest,
                    "create_time": version.create_time.isoformat() if version.create_time else None,
                }
                for version in versions
            ],
            "readonly": item.owner_id != actor.id,
        },
        datetime_format=False,
    )


@router.post("/{skill_id}/draft", summary="从已发布版本创建或恢复 Skill 草稿")
async def start_draft(
    skill_id: int, body: SkillDraftStartIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """普通打开复用草稿；显式恢复历史必须携带当前草稿修订号。"""
    item = await _owned(db, skill_id, actor.id)
    draft = await db.scalar(select(SkillDraft).where(SkillDraft.skill_id == skill_id))
    if draft is not None and body.from_version is None:
        return api_response(data=draft_output(draft), datetime_format=False)
    if draft is not None and draft.revision != body.expected_revision:
        raise CustomException(status_code=409, custom_code=10005, detail="恢复版本前须确认当前草稿修订号")
    async with resource_transaction(db, actor.id) as saved:
        files = await version_entries(db, item, body.from_version or item.current_version, saved)
        if draft is None:
            draft = SkillDraft(
                skill_id=skill_id,
                revision=1,
                base_version=item.current_version,
                files=files,
                settings={
                    "category": item.category,
                    "visibility": item.visibility,
                    "enabled": item.enabled,
                },
            )
            db.add(draft)
        else:
            await replace_draft(db, draft, files, body.expected_revision)
            draft.base_version = item.current_version
    await db.refresh(draft)
    return api_response(data=draft_output(draft), datetime_format=False)


@router.put("/{skill_id}/draft", summary="保存 Skill 文本设置、重命名或删除草稿文件")
async def save_draft(
    skill_id: int, body: SkillDraftUpdateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """只修改工作区，以修订号保护整批编辑，不改变已发布版本。"""
    item, draft = await _draft(db, skill_id, actor.id, body.expected_revision)
    async with resource_transaction(db, actor.id) as saved:
        files = dict(draft.files)
        for path in body.delete_paths:
            files.pop(package_path(path), None)
        for old, new in body.renames.items():
            old, new = package_path(old), package_path(new)
            if old == "SKILL.md" or old not in files or (new != old and new in files):
                raise ValueError("重命名目标已存在、源不存在或试图移动核心文件")
            if PurePosixPath(old).suffix.lower() != PurePosixPath(new).suffix.lower():
                raise ValueError("重命名须保留文件扩展名，修改文件类型请重新上传")
            files[new] = dict(files.pop(old))
            from app.services.skill_packages import file_kind

            files[new]["kind"] = file_kind(new)
        for path, text in body.files.items():
            path = package_path(path)
            data = text.encode()
            if files.get(path, {}).get("sha256") != hashlib.sha256(data).hexdigest():
                files[path] = await store_resource(db, actor.id, path, data, saved)
        settings = {
            **draft.settings,
            **body.model_dump(exclude_unset=True, include={"category", "visibility", "enabled"}),
        }
        if settings.get("visibility") is None or settings.get("enabled") is None:
            raise ValueError("可见性和启用设置不能为空")
        await replace_draft(db, draft, files, body.expected_revision, settings)
    await db.refresh(draft)
    return api_response(data=draft_output(draft), datetime_format=False)


@router.post("/{skill_id}/draft/files", summary="批量上传 Skill 草稿文件或目录资源")
async def upload_files(
    skill_id: int,
    expected_revision: int = Form(ge=1, description="预期草稿修订号"),
    paths: str = Form(default="[]", description="按上传顺序提供的逻辑路径 JSON 数组，空时使用文件名"),
    files: list[UploadFile] = File(description="同批文件，总请求最多 33 MiB"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """先限制实际文件读取，再一次性替换草稿并保留失败前的完整内容。"""
    item, draft = await _draft(db, skill_id, actor.id, expected_revision)
    try:
        async with resource_transaction(db, actor.id) as saved:
            names = json.loads(paths) or [file.filename for file in files]
            if not isinstance(names, list) or len(names) != len(files) or not 1 <= len(files) <= 128:
                raise ValueError("文件与逻辑路径必须一一对应，最多 128 个")
            if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                raise ValueError("本次上传包含重复路径")
            entries = dict(draft.files)
            for path, file in zip(names, files):
                path = package_path(path)
                entries[path] = await store_resource(db, actor.id, path, await file.read(MAX_RESOURCE_BYTES + 1), saved)
            await replace_draft(db, draft, entries, expected_revision)
    finally:
        for file in files:
            await file.close()
    await db.refresh(draft)
    return api_response(data=draft_output(draft), datetime_format=False)


@router.post("/{skill_id}/draft/import", summary="将完整 Skill ZIP 原子替换到现有草稿")
async def replace_package(
    skill_id: int,
    expected_revision: int = Form(ge=1, description="预期草稿修订号"),
    file: UploadFile = File(description="完整 Skill ZIP"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """先校验单根 ZIP 再替换草稿，已发布内容不受影响。"""
    item, draft = await _draft(db, skill_id, actor.id, expected_revision)
    try:
        async with resource_transaction(db, actor.id) as saved:
            resources = unpack_package(await file.read(MAX_PACKAGE_BYTES + 1))
            entries = {path: await store_resource(db, actor.id, path, data, saved) for path, data in resources.items()}
            await replace_draft(db, draft, entries, expected_revision)
    finally:
        await file.close()
    await db.refresh(draft)
    return api_response(data=draft_output(draft), datetime_format=False)


@router.get("/{skill_id}/files", summary="预览或下载指定草稿或发布版本的逻辑文件")
async def get_file(
    skill_id: int,
    path: str = Query(max_length=255, description="包内逻辑路径"),
    version: int | None = Query(default=None, ge=1, description="发布版本；不填读取所有者草稿"),
    download: bool = Query(default=False, description="是否使用附件下载方式"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """授权逻辑路径后读取固定正文，不暴露本地目录或对象存储键。"""
    try:
        path = package_path(path)
        if version is None:
            item, draft = await _draft(db, skill_id, actor.id)
            entries = draft.files
        else:
            owner = await db.scalar(select(Skill.owner_id).where(Skill.id == skill_id))
            item = await _owned(db, skill_id, actor.id) if owner == actor.id else (await selected_skills(db, [skill_id], actor.id))[0]
            if item.owner_id != actor.id and version != item.current_version:
                raise CustomException(status_code=404, custom_code=10002, detail="版本不存在")
            fixed = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == version))
            if fixed is None:
                raise CustomException(status_code=404, custom_code=10002, detail="版本不存在")
            entries = {entry["path"]: entry for entry in fixed.manifest if entry.get("package_file")}
            if not entries and path in fixed.files:
                return Response(fixed.files[path], media_type="text/plain")
        if path not in entries:
            raise CustomException(status_code=404, custom_code=10002, detail="文件不存在")
        entry = entries[path]
        raw = await read_resource(db, item.owner_id, entry)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    disposition = "attachment" if download or not entry.get("is_text") else "inline"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(PurePosixPath(path).name)}",
    }
    return Response(raw, media_type="text/plain" if entry.get("is_text") else entry["media_type"], headers=headers)


@router.post("/{skill_id}/draft/validate", summary="校验完整 Skill 草稿并定位发布阻断")
async def check_draft(
    skill_id: int, body: SkillDraftRevisionIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """校验结构、资源引用、模板及脚本验证状态，不修改已发布内容。"""
    item, draft = await _draft(db, skill_id, actor.id, body.expected_revision)
    try:
        report = await validate_draft(db, item, draft)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    return api_response(data=report, datetime_format=False)


@router.post("/{skill_id}/draft/publish", summary="原子发布经过校验的完整 Skill 文件包")
async def publish_draft(
    skill_id: int, body: SkillDraftRevisionIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """固定正文、模板和脚本绑定；同时检查草稿修订号与当前发布版本。"""
    item, draft = await _draft(db, skill_id, actor.id, body.expected_revision)
    async with resource_transaction(db, actor.id):
        report = await validate_draft(db, item, draft)
        if not report["valid"]:
            raise CustomException(status_code=409, custom_code=10005, detail="发布校验未通过：" + "；".join(report["errors"]))
        texts = await draft_texts(db, item, draft.files)
        name, description, _, _ = validate_package(
            {
                "SKILL.md": texts["SKILL.md"],
            }
        )
        next_version = draft.base_version + 1
        result = await db.execute(
            update(Skill)
            .where(Skill.id == skill_id, Skill.current_version == draft.base_version, Skill.is_deleted == 0)
            .values(**draft.settings, name=name, description=description, current_version=next_version)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CustomException(status_code=409, custom_code=10005, detail="已发布版本发生变化，请基于最新版创建草稿")
        # 草稿 CAS 与发布版本 CAS 位于同一事务；过期发布不会留下半个版本。
        await replace_draft(db, draft, draft.files, body.expected_revision)
        draft.base_version = next_version
        manifest = [dict(entry, path=path, package_file=True) for path, entry in sorted(draft.files.items())]
        for path, entry in draft.files.items():
            db.add(
                SkillVersionFile(
                    skill_id=skill_id, skill_version=next_version, path=path, artifact_id=entry["artifact_id"], sha256=entry["sha256"]
                )
            )
        for path, schema_path in report["config"].get("templates", {}).items():
            entry = draft.files[path]
            schema = validate_fields_schema(json.loads(texts[schema_path]))
            manifest.append(
                {
                    "path": path,
                    "filename": PurePosixPath(path).name,
                    "bytes": entry["bytes"],
                    "sha256": entry["sha256"],
                    "report_template": True,
                    "template_key": path.casefold(),
                    "artifact_id": entry["artifact_id"],
                    "fields_schema": schema,
                }
            )
            db.add(
                SkillTemplate(
                    skill_id=skill_id,
                    skill_version=next_version,
                    template_key=path.casefold(),
                    artifact_id=entry["artifact_id"],
                    fields_schema=schema,
                )
            )
        bindings = list(
            await db.scalars(
                select(SkillScript).where(SkillScript.skill_id == skill_id, SkillScript.package_hash == report["content_hash"])
            )
        )
        for binding in bindings:
            tool = await db.get(UserTool, binding.tool_id)
            tool.visibility, tool.enabled = draft.settings["visibility"], True
        db.add(
            SkillVersion(
                skill_id=skill_id,
                version=next_version,
                files=texts,
                manifest=manifest,
                content_hash=report["content_hash"],
                status=1,
                is_deleted=0,
            )
        )
        audit(db, "skill.package_published", actor_id=actor.id, target_id=skill_id)
    await db.refresh(item)
    return api_response(http_code=201, code=201, data=_output(item), datetime_format=False)


@router.get("/{skill_id}/versions/{version}/export", summary="导出包含二进制资源的完整 Skill 发布版本")
async def export_package(
    skill_id: int, version: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """按固定清单输出单根 ZIP，不包含凭据或服务器执行缓存。"""
    owner = await db.scalar(select(Skill.owner_id).where(Skill.id == skill_id))
    item = await _owned(db, skill_id, actor.id) if owner == actor.id else (await selected_skills(db, [skill_id], actor.id))[0]
    if item.owner_id != actor.id and version != item.current_version:
        raise CustomException(status_code=404, custom_code=10002, detail="版本不存在")
    fixed = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == version))
    if fixed is None:
        raise CustomException(status_code=404, custom_code=10002, detail="版本不存在")
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            entries = [entry for entry in fixed.manifest if entry.get("package_file")]
            if entries:
                for entry in entries:
                    archive.writestr(f"{item.name}/{entry['path']}", await read_resource(db, item.owner_id, entry))
            else:
                for path, content in fixed.files.items():
                    archive.writestr(f"{item.name}/{path}", content)
    except ValueError as exc:
        raise CustomException(status_code=409, custom_code=10005, detail=str(exc)) from None
    return Response(
        output.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{item.name}-v{version}.zip"',
        },
    )


async def _script_binding(db: AsyncSession, item: Skill, draft: SkillDraft, path: str) -> tuple[SkillScript, UserToolVersion]:
    """为精确包摘要创建内部 Tool，复用既有依赖准备队列和试运行状态。"""
    texts = await draft_texts(db, item, draft.files)
    config = execution_config(texts)
    if path not in config["scripts"]:
        raise ValueError("脚本未在 y-point.yaml 中登记")
    fields = script_fields(path, config["scripts"][path], texts)
    digest = package_digest(draft.files)
    binding = await db.scalar(
        select(SkillScript).where(SkillScript.skill_id == item.id, SkillScript.package_hash == digest, SkillScript.path == path)
    )
    if binding:
        version = await db.scalar(
            select(UserToolVersion).where(UserToolVersion.tool_id == binding.tool_id, UserToolVersion.version == binding.tool_version)
        )
        return binding, version
    tool = UserTool(
        skill_id=item.id,
        owner_id=item.owner_id,
        name=f"skill_{item.id}_{digest[:16]}_{hashlib.sha256(path.encode()).hexdigest()[:8]}",
        description=f"{item.name}: {path}",
        kind=ToolKind.SCRIPT,
        visibility=Visibility.PRIVATE,
        enabled=False,
        current_version=1,
        is_deleted=0,
        status=1,
    )
    db.add(tool)
    await db.flush()
    requires_approval = fields.pop("requires_approval")
    effect = fields.pop("effect")
    version = UserToolVersion(
        tool_id=tool.id,
        version=1,
        content_hash=digest,
        effect=effect,
        requires_approval=requires_approval,
        verified=False,
        is_deleted=0,
        status=1,
        **initial_environment_fields(fields["requirements"]),
        **fields,
    )
    binding = SkillScript(skill_id=item.id, package_hash=digest, path=path, tool_id=tool.id, tool_version=1)
    db.add_all([version, binding])
    await db.commit()
    return binding, version


@router.post("/{skill_id}/draft/scripts/prepare", summary="准备当前 Skill 草稿脚本的固定依赖环境")
async def prepare_script(
    skill_id: int, body: SkillScriptTestIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """把包内脚本接入已有 worker 依赖准备队列，不执行用户代码。"""
    item, draft = await _draft(db, skill_id, actor.id, body.expected_revision)
    try:
        binding, version = await _script_binding(db, item, draft, body.path)
        if version.environment_state == ToolEnvironmentState.FAILED:
            for key, value in initial_environment_fields(version.requirements).items():
                setattr(version, key, value)
            await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="脚本准备状态已变化，请刷新后重试") from None
    except (ValueError, yaml.YAMLError) as exc:
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    return api_response(
        data={
            "path": binding.path,
            "environment": environment_output(version),
            "verified": version.verified,
        },
        datetime_format=False,
    )


@router.post("/{skill_id}/draft/scripts/test", summary="在隔离沙箱中试运行当前 Skill 草稿脚本")
async def test_script(
    skill_id: int, body: SkillScriptTestIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)
):
    """只读挂载精确包资源，参数验证、并发名额和执行上限沿用 Tool 治理。"""
    item, draft = await _draft(db, skill_id, actor.id, body.expected_revision)
    try:
        binding, version = await _script_binding(db, item, draft, body.path)
        Draft202012Validator(version.input_schema).validate(body.arguments)
        environment = await require_script_environment(version)
        resources = {path: await read_resource(db, item.owner_id, entry) for path, entry in draft.files.items()}
        sessions = async_sessionmaker(db.bind, expire_on_commit=False)
        timeout = get_config().SCRIPT_TEST_TIMEOUT_SECONDS
        async with tool_execution_slot(sessions, actor.id, None, ToolLeaseKind.CALL, timeout):
            result = await asyncio.wait_for(
                run_script(
                    version.script_language,
                    version.source_code,
                    body.arguments,
                    timeout,
                    environment=environment,
                    package_files=resources,
                    entrypoint=body.path,
                ),
                timeout=timeout,
            )
        await db.refresh(draft)
        if draft.revision != body.expected_revision or package_digest(draft.files) != binding.package_hash:
            raise CustomException(status_code=409, custom_code=10005, detail="试运行期间草稿已变化，结果不作为新版验证")
        version.verified = True
        audit(db, "skill.script_tested", actor_id=actor.id, target_id=skill_id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="脚本准备状态已变化，请刷新后重试") from None
    except ToolCapacityError as exc:
        raise CustomException(status_code=429, custom_code=10005, detail=str(exc)) from None
    except (ValueError, ValidationError, OSError, TimeoutError, yaml.YAMLError) as exc:
        raise CustomException(status_code=400, custom_code=10006, detail="脚本试运行失败或配置未就绪：" + str(exc)[:200]) from None
    return api_response(
        data={
            "path": body.path,
            "result": result,
            "verified": True,
        },
        datetime_format=False,
    )


@router.get("/{skill_id}/files/preview", summary="预览 Skill 草稿或发布版本的文档资源")
async def preview_file(
    skill_id: int,
    path: str = Query(max_length=255, description="包内逻辑文件路径"),
    version: int | None = Query(default=None, ge=1, description="发布版本，不填读取本人草稿"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """复用同一文件授权和完整性检查，以隔离内容预览呈现模板和参考文档。"""
    from app.services.artifact_preview import PREVIEW_MEDIA_TYPES, build_artifact_preview

    response = await get_file(skill_id, path, version, False, actor, db)
    media_type = response.media_type
    if media_type not in PREVIEW_MEDIA_TYPES:
        raise CustomException(status_code=415, custom_code=10006, detail="该资源暂不支持内容预览，请下载原件查看")
    try:
        preview = await asyncio.to_thread(build_artifact_preview, "skill-resource", PurePosixPath(path).name, media_type, response.body)
    except Exception:
        raise CustomException(status_code=422, custom_code=10006, detail="资源无法预览，请下载原件查看") from None
    result = api_response(data=preview.model_dump(mode="json"), is_pop=False)
    result.headers["Cache-Control"] = "no-store"
    result.headers["X-Content-Type-Options"] = "nosniff"
    return result
