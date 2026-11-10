# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 10:33:31
# @Author  : yangyuexiong
# @File    : skills.py

"""Skill 创建、版本编辑、导入导出和访问控制接口。"""

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import Visibility
from app.core.custom_exception import CustomException
from app.core.response import api_response
from app.core.security import get_current_admin
from app.db.session import get_db_session
from app.models.admin import Admin
from app.models.skill import Skill, SkillVersion
from app.schemas.skill import SkillCreateIn, SkillUpdateIn
from app.services.identity import audit
from app.services.skills import MAX_ZIP_BYTES, TEMPLATE, export_zip, import_zip, validate_package

router = APIRouter(prefix="/skills", tags=["skills"])


def _output(skill: Skill, version: SkillVersion | None = None) -> dict:
    """仅向所有者返回正文，公开列表只展示元数据。"""
    result = {
        "id": skill.id,
        "owner_id": skill.owner_id,
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "visibility": skill.visibility,
        "enabled": skill.enabled,
        "current_version": skill.current_version,
    }
    if version is not None:
        result.update(files=version.files, manifest=version.manifest, content_hash=version.content_hash)
        from app.services.skill_reports import template_key

        templates = [
            {
                **entry,
                "template_key": entry.get("template_key") or template_key(entry["filename"]),
            }
            for entry in version.manifest
            if entry.get("report_template") is True
        ]
        result["report_templates"] = templates
        result["report_template"] = templates[0] if len(templates) == 1 else None
    return result


async def _owned(db: AsyncSession, skill_id: int, owner_id: int, lock: bool = False) -> Skill:
    """按所有者查找仍存在的 Skill。"""
    statement = select(Skill).where(Skill.id == skill_id, Skill.owner_id == owner_id, Skill.is_deleted == 0)
    item = await db.scalar(statement.with_for_update() if lock else statement)
    if item is None:
        raise CustomException(status_code=404, custom_code=10002, detail="Skill 不存在")
    return item


async def _version(db: AsyncSession, skill: Skill) -> SkillVersion:
    """读取当前完整包版本。"""
    return await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == skill.current_version))


async def _create(db: AsyncSession, owner_id: int, body: SkillCreateIn) -> dict:
    """验证内容并创建初始不可变版本。"""
    try:
        name, description, manifest, digest = validate_package(body.files)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    item = Skill(
        owner_id=owner_id,
        name=name,
        description=description,
        category=body.category,
        visibility=body.visibility,
        enabled=True,
        current_version=1,
        is_deleted=0,
        status=1,
    )
    db.add(item)
    try:
        await db.flush()
        db.add(SkillVersion(skill_id=item.id, version=1, content_hash=digest, files=body.files, manifest=manifest, is_deleted=0, status=1))
        audit(db, "skill.created", actor_id=owner_id, target_id=item.id)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise CustomException(status_code=409, custom_code=10005, detail="同名 Skill 已存在") from None
    return _output(item, await _version(db, item))


@router.get("", summary="列出可见 Skill")
async def list_skills(actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """公开 Skill 与本人 Skill 对所有用户可见，正文不在列表中返回。"""
    items = (
        await db.scalars(
            select(Skill)
            .where(Skill.is_deleted == 0, or_(Skill.owner_id == actor.id, (Skill.visibility == Visibility.PUBLIC) & Skill.enabled.is_(True)))
            .order_by(Skill.id.desc()),
        )
    ).all()
    return api_response(data=[_output(item) for item in items], is_pop=False)


@router.get("/template", summary="下载标准 Skill 模板")
async def download_template(actor: Admin = Depends(get_current_admin)):
    """导出可自行编辑并再次导入的 ZIP 模板。"""
    return Response(
        export_zip(
            {
                "SKILL.md": TEMPLATE + "\n参考资料见 [设计说明](references/architecture.md)。产物与脚本执行约定见 README.md。\n",
                "references/architecture.md": "# 参考资料\n\n在此记录任务需要读取的规范、材料或接口说明。\n",
                "scripts/validate.py": 'import json, sys\nprint(json.dumps({"valid": bool(json.load(sys.stdin))}))\n',
                "assets/templates/README.md": "# 产物模板\n\n上传 DOCX 后，在 y-point.yaml 中关联此路径与 assets/schemas 中的字段契约。\n",
                "assets/schemas/report.schema.json": '{"type":"object","properties":{"title":{"type":"string"}},"required":["title"]}',
                "assets/examples/report.json": '{"title":"示例报告"}',
                "y-point.yaml": "templates: {}\nscripts: {}\nrequired_tools: []\n",
                "README.md": "# Skill 使用说明\n\n先编写 SKILL.md，再添加资料、模板和字段契约。脚本声明后必须试运行再发布。\n",
            },
        ),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="skill-template.zip"',
        },
    )


@router.post("/import", summary="导入 Skill ZIP")
async def upload_skill(
    file: UploadFile = File(...),
    visibility: str | None = Query(None, pattern="^(private|public)$"),
    actor: Admin = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db_session),
):
    """有界读取 ZIP，校验完整文件清单后创建 Skill。"""
    data = await file.read(MAX_ZIP_BYTES + 1)
    try:
        files = import_zip(data)
    except ValueError as exc:
        raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
    name, _, _, _ = validate_package(files)
    existing = await db.scalar(select(Skill).where(Skill.owner_id == actor.id, Skill.name == name, Skill.is_deleted == 0))
    if existing is not None:
        changes = {
            "expected_version": existing.current_version,
            "files": files,
        }
        if visibility is not None:
            changes["visibility"] = visibility
        return await update_skill(existing.id, SkillUpdateIn(**changes), actor, db)
    return api_response(
        http_code=201,
        code=201,
        data=await _create(db, actor.id, SkillCreateIn(files=files, visibility=visibility or Visibility.PRIVATE)),
        datetime_format=False,
    )


@router.post("", summary="创建 Skill")
async def create_skill(body: SkillCreateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """由标准 SKILL.md 创建私有或公开 Skill。"""
    return api_response(http_code=201, code=201, data=await _create(db, actor.id, body), datetime_format=False)


@router.get("/{skill_id}", summary="查询 Skill 详情")
async def get_skill(skill_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """所有者可编辑正文；其他用户仅可读取公开元数据。"""
    item = await db.get(Skill, skill_id)
    if item is None or item.is_deleted != 0 or (item.owner_id != actor.id and (item.visibility != Visibility.PUBLIC or not item.enabled)):
        raise CustomException(status_code=404, custom_code=10002, detail="Skill 不存在")
    return api_response(data=_output(item, await _version(db, item) if item.owner_id == actor.id else None), datetime_format=False)


@router.get("/{skill_id}/export", summary="导出 Skill ZIP")
async def download_skill(skill_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """所有者或公开 Skill 的使用者可下载当前版本。"""
    item = await db.get(Skill, skill_id)
    if item is None or item.is_deleted != 0 or (item.owner_id != actor.id and (item.visibility != Visibility.PUBLIC or not item.enabled)):
        raise CustomException(status_code=404, custom_code=10002, detail="Skill 不存在")
    version = await _version(db, item)
    if version is None:
        raise CustomException(status_code=409, custom_code=10005, detail="请先在工作区发布 Skill")
    if any(entry.get("package_file") for entry in version.manifest):
        from app.api.v1.skill_packages import export_package

        return await export_package(skill_id, item.current_version, actor, db)
    return Response(
        export_zip(version.files),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{item.name}.zip"',
        },
    )


@router.put("/{skill_id}", summary="更新 Skill")
async def update_skill(skill_id: int, body: SkillUpdateIn, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """修改内容时创建新版本；Agent 已关联的版本保持固定。"""
    item = await _owned(db, skill_id, actor.id, lock=True)
    if item.current_version != body.expected_version:
        raise CustomException(status_code=409, custom_code=10005, detail="Skill 版本已变化")
    previous = await _version(db, item)
    if previous is None or any(entry.get("package_file") for entry in previous.manifest):
        raise CustomException(status_code=409, custom_code=10005, detail="文件包请在 Skill 工作区编辑草稿并发布")
    changes = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if any(changes.get(key) is None for key in ("visibility", "enabled") if key in changes):
        raise CustomException(status_code=400, custom_code=10005, detail="必填设置不能为空")
    if "files" in changes:
        if changes["files"] is None:
            raise CustomException(status_code=400, custom_code=10005, detail="文件不能为空")
        try:
            name, description, manifest, digest = validate_package(changes["files"])
        except ValueError as exc:
            raise CustomException(status_code=400, custom_code=10005, detail=str(exc)) from exc
        if name != item.name:
            raise CustomException(status_code=400, custom_code=10005, detail="Skill 名称不可修改")
        # 描述随正文版本在下方比较并交换事务中更新。
        files = changes["files"]
    else:
        previous = await _version(db, item)
        files, manifest, digest = previous.files, previous.manifest, previous.content_hash
    if changes:
        from app.services.skill_reports import copy_template, report_package_hash

        template_entries = [entry for entry in previous.manifest if entry.get("report_template") is True]
        if template_entries:
            manifest = [entry for entry in manifest if entry.get("report_template") is not True] + template_entries
            digest = report_package_hash(files, template_entries)
        settings = {key: changes[key] for key in ("category", "visibility", "enabled") if key in changes}
        if "files" in changes:
            settings["description"] = description
        result = await db.execute(
            update(Skill)
            .where(
                Skill.id == item.id,
                Skill.current_version == body.expected_version,
                Skill.is_deleted == 0,
            )
            .values(**settings, current_version=body.expected_version + 1)
            .execution_options(synchronize_session=False),
        )
        if result.rowcount != 1:
            await db.rollback()
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 版本已变化")
        await copy_template(db, item.id, body.expected_version, body.expected_version + 1)
        db.add(
            SkillVersion(
                skill_id=item.id,
                version=body.expected_version + 1,
                files=files,
                manifest=manifest,
                content_hash=digest,
                is_deleted=0,
                status=1,
            ),
        )
    audit(db, "skill.updated", actor_id=actor.id, target_id=item.id)
    await db.commit()
    await db.refresh(item)
    return api_response(data=_output(item, await _version(db, item)), datetime_format=False)


@router.delete("/{skill_id}", summary="删除 Skill")
async def delete_skill(skill_id: int, actor: Admin = Depends(get_current_admin), db: AsyncSession = Depends(get_db_session)):
    """逻辑删除并立即禁止后续运行装载。"""
    item = await _owned(db, skill_id, actor.id, lock=True)
    item.is_deleted = 1
    item.enabled = False
    item.name = f"_deleted_{item.id}"
    audit(db, "skill.deleted", actor_id=actor.id, target_id=item.id)
    await db.commit()
    return api_response(message="Skill 已删除")
