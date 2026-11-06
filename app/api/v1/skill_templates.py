# -*- coding: utf-8 -*-
# @Time    : 2026-11-05 10:02:28
# @Author  : yangyuexiong
# @File    : skill_templates.py

"""按 Skill 不可变版本管理一个或多个 DOCX 模板资源。"""

import json

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.skills import _owned, _output, _version
from app.api.v1.artifacts import _cleanup_failed_uploads
from app.core.custom_exception import CustomException
from app.core.enums import ArtifactOrigin
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.skill import Skill, SkillVersion
from app.models.skill_template import SkillTemplate
from app.services.artifact_store import ArtifactValidationError, artifact_root, save_upload, enforce_owner_quota
from app.services.identity import audit
from app.services.skill_reports import (
    MAX_SKILL_TEMPLATES,
    MAX_TEMPLATE_BYTES,
    report_package_hash,
    template_key,
    validate_docx_template,
    validate_fields_schema,
)

router = APIRouter(prefix="/skills", tags=["skills"])


async def _publish_templates(
    skill_id: int,
    expected_version: int,
    files: list[UploadFile],
    fields_schema: str,
    actor: Admin,
    db: AsyncSession,
    *,
    legacy_single: bool = False,
):
    """先校验全部文件，再原子发布新增或按名称替换的完整模板集合。"""
    item = await _owned(db, skill_id, actor.id, lock=True)
    if item.current_version != expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="Skill 版本已变化")
    previous = await _version(db, item)
    if previous is None or any(entry.get("package_file") for entry in previous.manifest):
        raise CustomException(status_code=409, custom_code=10005, detail="文件包模板请在工作区修改并与草稿一起发布")
    existing = [entry for entry in previous.manifest if entry.get("report_template") is True]
    if legacy_single and len(existing) > 1:
        raise CustomException(status_code=409, custom_code=10005, detail="此 Skill 已有多个模板，请使用批量模板接口")
    try:
        if not 1 <= len(files) <= MAX_SKILL_TEMPLATES:
            raise ValueError(f"每次可上传 1 至 {MAX_SKILL_TEMPLATES} 个模板")
        if len(fields_schema.encode()) > 32 * 1024 * len(files):
            raise ValueError("每个模板的字段契约最多 32 KiB")
        schemas = json.loads(fields_schema)
        if legacy_single:
            schemas = {
                files[0].filename: schemas,
            }
        names = [file.filename or "" for file in files]
        keys = [template_key(name) for name in names]
        if len(set(keys)) != len(keys):
            raise ValueError("本次上传包含同名模板，文件名不区分大小写")
        if not isinstance(schemas, dict) or set(schemas) != set(names):
            raise ValueError("fields_schema 必须按每个上传文件的原始文件名提供独立字段契约")
        validated = []
        for file, key in zip(files, keys):
            schema = validate_fields_schema(schemas[file.filename])
            if len(json.dumps(schema).encode()) > 32 * 1024:
                raise ValueError("每个模板的字段契约最多 32 KiB")
            data = await file.read(MAX_TEMPLATE_BYTES + 1)
            validate_docx_template(data, schema)
            await file.seek(0)
            validated.append((file, key, schema))
        entries = {} if legacy_single else {template_key(entry["filename"]): entry for entry in existing}
        if len(set(entries) | set(keys)) > MAX_SKILL_TEMPLATES:
            raise ValueError(f"每个 Skill 版本最多 {MAX_SKILL_TEMPLATES} 个模板")
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from exc
    except Exception as exc:
        raise CustomException(status_code=400, custom_code=10006, detail="模板或字段契约无效，请检查 DOCX、占位符及字段类型") from exc
    saved = []
    root = artifact_root()
    try:
        for file, key, schema in validated:
            artifact, path = await save_upload(file, actor.id, root)
            saved.append((artifact, path))
            artifact.source = ArtifactOrigin.SKILL_RESOURCE
            artifact.expires_at = None
            entries[key] = {
                "path": f"templates/{artifact.filename}",
                "bytes": artifact.byte_size,
                "report_template": True,
                "template_key": key,
                "artifact_id": artifact.id,
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "fields_schema": schema,
            }
        await enforce_owner_quota(db, actor.id, add_count=len(saved), add_bytes=sum(artifact.byte_size for artifact, _ in saved))
        result = await db.execute(
            update(Skill)
            .where(Skill.id == skill_id, Skill.current_version == expected_version, Skill.is_deleted == 0)
            .values(current_version=expected_version + 1)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 版本已变化")
        resources = [entries[key] for key in sorted(entries)]
        manifest = [entry for entry in previous.manifest if entry.get("report_template") is not True] + resources
        db.add_all([artifact for artifact, _ in saved])
        db.add(
            SkillVersion(
                skill_id=skill_id,
                version=expected_version + 1,
                files=previous.files,
                manifest=manifest,
                content_hash=report_package_hash(previous.files, resources),
                is_deleted=0,
                status=1,
            )
        )
        db.add_all(
            [
                SkillTemplate(
                    skill_id=skill_id,
                    skill_version=expected_version + 1,
                    template_key=entry.get("template_key") or template_key(entry["filename"]),
                    artifact_id=entry["artifact_id"],
                    fields_schema=entry["fields_schema"],
                    is_deleted=0,
                    status=1,
                )
                for entry in resources
            ]
        )
        audit(db, "skill.templates_published", actor_id=actor.id, target_id=skill_id)
        await db.commit()
    except ArtifactValidationError as exc:
        await db.rollback()
        await _cleanup_failed_uploads(saved, root)
        raise CustomException(status_code=400, custom_code=10006, detail=str(exc)) from None
    except BaseException:
        await db.rollback()
        await _cleanup_failed_uploads(saved, root)
        raise
    await db.refresh(item)
    return api_response(http_code=201, code=201, data=_output(item, await _version(db, item)), datetime_format=False)


@router.post("/{skill_id}/templates", summary="批量发布 Skill 的 DOCX 模板新版本")
async def publish_templates(
    skill_id: int,
    expected_version: int = Form(ge=1, description="当前 Skill 版本"),
    fields_schema: str = Form(description="以每个上传文件名为键、独立 JSON Schema 为值的对象"),
    files: list[UploadFile] = File(description="本批 DOCX 模板，最多 10 个、每个最多 2 MiB"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """新增模板保留已有资源；同名文件只在新版本中替换，整批成功或失败。"""
    return await _publish_templates(skill_id, expected_version, files, fields_schema, actor, db)


@router.post("/{skill_id}/template", summary="兼容发布 Skill 的单个 DOCX 模板")
async def publish_template(
    skill_id: int,
    expected_version: int = Form(ge=1, description="当前 Skill 版本"),
    fields_schema: str = Form(description="单个报告字段的 JSON Schema 对象"),
    file: UploadFile = File(description="包含字段占位符的 DOCX 模板，最多 2 MiB"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """兼容旧单模板替换语义，多模板版本拒绝旧接口以免误删资源。"""
    return await _publish_templates(skill_id, expected_version, [file], fields_schema, actor, db, legacy_single=True)
