# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:05:30
# @Author  : yangyuexiong
# @File    : skill_packages.py

"""统一管理 Skill 文件包、草稿资源、不可变发布和执行配置。"""

import hashlib
import io
import json
import os
import re
import stat
import unicodedata
import zipfile
from pathlib import PurePosixPath
from uuid import uuid4

import yaml
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.enums import ArtifactOrigin, ArtifactState, SkillFileKind, Visibility
from app.models.artifact import Artifact
from app.models.skill import Skill, SkillVersion
from app.models.base import shanghai_now
from app.models.skill_package import SkillDraft, SkillScript, SkillVersionFile
from app.models.skill_template import SkillTemplate
from app.models.tool import UserToolVersion
from app.services.artifact_store import artifact_root, enforce_owner_quota, scan_file, validate_file
from app.services.private_blob_store import delete_blob, read_blob, store_blob, using_s3
from app.services.skill_reports import MAX_SKILL_TEMPLATES, validate_docx_template, validate_fields_schema
from app.services.skills import validate_package

MAX_PACKAGE_FILES = 128
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_RESOURCE_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 128 * 1024
TEXT_SUFFIXES = {".md", ".txt", ".py", ".sh", ".json", ".yaml", ".yml", ".csv"}
CONFIG_PATH = "y-point.yaml"


def package_path(path: str) -> str:
    """规范化包内路径，拒绝目录逃逸、隐藏文件和平台歧义。"""
    if not isinstance(path, str):
        raise ValueError("文件路径必须是字符串")
    value = unicodedata.normalize("NFC", path)
    parts = value.split("/")
    if (
        not value
        or len(value) > 255
        or PurePosixPath(value).as_posix() != value
        or "\\" in value
        or any(not part or part.startswith(".") or ":" in part or len(part) > 128 for part in parts)
        or value.startswith("/")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("文件路径必须是无隐藏项、无目录跳转的相对路径")
    return value


def file_kind(path: str) -> SkillFileKind:
    """按逻辑目录返回展示类别，目录位置本身不授予执行能力。"""
    if path == "SKILL.md":
        return SkillFileKind.INSTRUCTION
    if path.startswith("references/"):
        return SkillFileKind.REFERENCE
    if path.startswith("scripts/"):
        return SkillFileKind.SCRIPT
    if path.startswith("assets/templates/"):
        return SkillFileKind.TEMPLATE
    if path.startswith("assets/schemas/"):
        return SkillFileKind.SCHEMA
    return SkillFileKind.TEXT if PurePosixPath(path).suffix.lower() in TEXT_SUFFIXES else SkillFileKind.ASSET


def check_entries(files: dict) -> None:
    """约束完整包的数量、总字节和大小写及前缀冲突。"""
    if len(files) > MAX_PACKAGE_FILES or sum(entry["bytes"] for entry in files.values()) > MAX_PACKAGE_BYTES:
        raise ValueError("Skill 包最多 128 个文件，总大小最多 32 MiB")
    keys = set()
    for path in files:
        key = package_path(path).casefold()
        if key in keys:
            raise ValueError("文件名不区分大小写，包中存在重复路径")
        keys.add(key)
    for key in keys:
        if any("/".join(key.split("/")[:index]) in keys for index in range(1, len(key.split("/")))):
            raise ValueError("文件与目录路径冲突")


def package_digest(files: dict) -> str:
    """只依据逻辑路径与内容摘要计算与存储位置无关的完整包指纹。"""
    entries = [(path, item["sha256"], item["bytes"]) for path, item in sorted(files.items())]
    return hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode()).hexdigest()


async def read_resource(db: AsyncSession, owner_id: int, entry: dict) -> bytes:
    """仅按受控清单读取同所有者资源，核对状态、大小和摘要。"""
    item = await db.get(Artifact, entry["artifact_id"])
    if (
        item is None
        or item.owner_id != owner_id
        or item.source != ArtifactOrigin.SKILL_RESOURCE
        or item.state != ArtifactState.READY
        or item.is_deleted != 0
        or item.status != 1
        or item.sha256 != entry["sha256"]
        or item.byte_size != entry["bytes"]
    ):
        raise ValueError("Skill 文件资源不可用或已变化")
    try:
        data = await read_blob(artifact_root(), item.storage_key, max_bytes=MAX_RESOURCE_BYTES)
    except (OSError, BotoCoreError, ClientError):
        raise ValueError("Skill 文件正文暂不可读，请稍后重试") from None
    if len(data) != item.byte_size or hashlib.sha256(data).hexdigest() != item.sha256:
        raise ValueError("Skill 文件完整性校验失败")
    return data


async def store_resource(db: AsyncSession, owner_id: int, path: str, data: bytes, saved: list) -> dict:
    """将文本或二进制正文写入既有私有存储，失败清理由调用事务负责。"""
    path = package_path(path)
    suffix = PurePosixPath(path).suffix.lower()
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError("Skill 单文件最多 10 MiB")
    if suffix in TEXT_SUFFIXES:
        if len(data) > MAX_TEXT_BYTES or b"\x00" in data:
            raise ValueError("文本文件最多 128 KiB，且不能含空字符")
        data.decode("utf-8")
    root = artifact_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = uuid4().hex
    target = root / key
    artifact = Artifact(
        id=str(uuid4()),
        owner_id=owner_id,
        filename=PurePosixPath(path).name,
        media_type="text/plain",
        byte_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        storage_key=key,
        source=ArtifactOrigin.SKILL_RESOURCE,
        state=ArtifactState.READY,
        expires_at=None,
        status=1,
        is_deleted=0,
    )
    saved.append((artifact, target))
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)
    if suffix in TEXT_SUFFIXES:
        artifact.media_type = "text/markdown" if suffix == ".md" else "text/plain"
    else:
        artifact.media_type = validate_file(target, artifact.filename)
    await scan_file(target)
    if using_s3():
        await store_blob(root, key, target)
        target.unlink(missing_ok=True)
    return {
        "artifact_id": artifact.id,
        "sha256": artifact.sha256,
        "bytes": len(data),
        "media_type": artifact.media_type,
        "is_text": suffix in TEXT_SUFFIXES,
        "kind": file_kind(path),
    }


async def cleanup_resources(saved: list) -> None:
    """回滚后尽力清理新资源，保持原始业务错误。"""
    for item, target in saved:
        try:
            target.unlink(missing_ok=True)
            await delete_blob(artifact_root(), item.storage_key)
        except Exception:
            # 对象存储临时失败由既有孤儿清理流程后续收敛。
            pass


async def version_entries(db: AsyncSession, skill: Skill, number: int, saved: list) -> dict:
    """读取已发布文件清单；旧文字包和独立模板只在新草稿中迁移资源引用。"""
    version = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == number))
    if version is None:
        if number == 0:
            return {}
        raise CustomException(status_code=404, custom_code=10002, detail="Skill 版本不存在")
    resources = [entry for entry in version.manifest if entry.get("package_file")]
    if resources:
        return {entry["path"]: {key: value for key, value in entry.items() if key not in {"path", "package_file"}} for entry in resources}
    result = {}
    for path, text in version.files.items():
        result[path] = await store_resource(db, skill.owner_id, path, text.encode(), saved)
    template_map = {}
    for entry in version.manifest:
        if entry.get("report_template"):
            path = f"assets/templates/{entry['filename']}"
            result[path] = {
                "artifact_id": entry["artifact_id"],
                "sha256": entry["sha256"],
                "bytes": entry["bytes"],
                "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "is_text": False,
                "kind": SkillFileKind.TEMPLATE,
            }
            schema_path = f"assets/schemas/{PurePosixPath(entry['filename']).stem}.schema.json"
            result[schema_path] = await store_resource(
                db, skill.owner_id, schema_path, json.dumps(entry["fields_schema"], ensure_ascii=False, indent=2).encode(), saved
            )
            template_map[path] = schema_path
    if template_map:
        config = {
            "templates": template_map,
            "scripts": {},
            "required_tools": [],
        }
        result[CONFIG_PATH] = await store_resource(
            db, skill.owner_id, CONFIG_PATH, yaml.safe_dump(config, allow_unicode=True).encode(), saved
        )
    check_entries(result)
    return result


async def draft_texts(db: AsyncSession, skill: Skill, files: dict) -> dict[str, str]:
    """读取有大小限制的文本文件，二进制资源只保留引用。"""
    return {path: (await read_resource(db, skill.owner_id, entry)).decode("utf-8") for path, entry in files.items() if entry.get("is_text")}


def execution_config(texts: dict[str, str]) -> dict:
    """解析可携带的执行配置，拒绝未知能力与无法校验的外部引用。"""
    raw = texts.get(CONFIG_PATH, "{}")
    # 拒绝 YAML 别名，避免循环引用和高倍展开的配置进入执行定义。
    if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(raw)):
        raise ValueError("执行配置不支持 YAML 锚点和别名")
    config = yaml.safe_load(raw) or {}
    if not isinstance(config, dict) or set(config) - {"templates", "scripts", "required_tools"}:
        raise ValueError("y-point.yaml 只支持 templates、scripts 和 required_tools")
    templates, scripts, required = config.get("templates", {}), config.get("scripts", {}), config.get("required_tools", [])
    if not isinstance(templates, dict) or len(templates) > MAX_SKILL_TEMPLATES:
        raise ValueError("templates 必须是最多 10 个模板路径到 Schema 路径的映射")
    if not isinstance(scripts, dict) or len(scripts) > 8:
        raise ValueError("每个 Skill 最多配置 8 个包内脚本")
    if not isinstance(required, list) or len(required) > 16 or any(not isinstance(name, str) or len(name) > 64 for name in required):
        raise ValueError("required_tools 必须是最多 16 个工具名称")
    for path, schema_path in templates.items():
        package_path(path)
        package_path(schema_path)
    for path, spec in scripts.items():
        package_path(path)
        if not isinstance(spec, dict):
            raise ValueError("scripts 中的脚本配置必须是对象")
    if len(set(required)) != len(required) or any(not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) for name in required):
        raise ValueError("必需工具名称不能重复，须为已注册的工具名称")
    return {
        "templates": templates,
        "scripts": scripts,
        "required_tools": required,
    }


async def validate_draft(db: AsyncSession, skill: Skill, draft: SkillDraft, *, check_scripts: bool = True) -> dict:
    """返回可定位的发布阻断和提示，不因普通编辑中间态删除草稿。"""
    errors, warnings, script_statuses = [], [], []
    texts = await draft_texts(db, skill, draft.files)
    config = {}
    try:
        check_entries(draft.files)
        # 旧文字包校验仅用于核心元信息，完整文件预算由包校验负责。
        name, _, _, _ = validate_package(
            {
                "SKILL.md": texts.get("SKILL.md", ""),
            }
        )
        if skill.current_version > 0 and name != skill.name:
            raise ValueError("已发布 Skill 名称不可修改，请复制为新的 Skill")
        if skill.current_version == 0 and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None:
            raise ValueError("新包名称仅支持小写字母、数字和单个连字符分隔")
        config = execution_config(texts)
    except (ValueError, yaml.YAMLError) as exc:
        errors.append(str(exc))
    for path, schema_path in config.get("templates", {}).items():
        try:
            if package_path(path) not in draft.files or not path.startswith("assets/templates/"):
                raise ValueError("模板必须引用 assets/templates/ 中的文件")
            if package_path(schema_path) not in texts or not schema_path.startswith("assets/schemas/"):
                raise ValueError("字段契约必须引用 assets/schemas/ 中的文本文件")
            schema = validate_fields_schema(json.loads(texts[schema_path]))
            validate_docx_template(await read_resource(db, skill.owner_id, draft.files[path]), schema)
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(f"{path}: {exc}")
        except Exception:
            errors.append(f"{path}: 模板内容或字段契约无法解析，请检查 DOCX、占位符和字段类型")
    digest = package_digest(draft.files)
    for path, spec in config.get("scripts", {}).items():
        try:
            script_fields(path, spec, texts)
            if check_scripts:
                binding = await db.scalar(
                    select(SkillScript).where(
                        SkillScript.skill_id == skill.id, SkillScript.package_hash == digest, SkillScript.path == path
                    )
                )
                version = (
                    await db.scalar(
                        select(UserToolVersion).where(
                            UserToolVersion.tool_id == binding.tool_id, UserToolVersion.version == binding.tool_version
                        )
                    )
                    if binding
                    else None
                )
                from app.services.tool_environments import environment_output

                script_statuses.append(
                    {
                        "path": path,
                        "verified": bool(version and version.verified),
                        "environment": environment_output(version) if version else None,
                    }
                )
                if version is None or not version.verified:
                    raise ValueError("当前文件包内容尚未通过脚本试运行，请准备依赖并重新试运行")
        except (ValueError, TypeError) as exc:
            errors.append(f"{path}: {exc}")
    for path in draft.files:
        if path.startswith("assets/templates/") and path.endswith(".docx") and path not in config.get("templates", {}):
            warnings.append(f"{path}: 尚未关联字段契约，不会成为报告模板")
        if path.startswith("scripts/") and path not in config.get("scripts", {}):
            warnings.append(f"{path}: 未登记执行配置，仅作为只读文件")
    for path, text in texts.items():
        if path.endswith(".md"):
            for reference in re.findall(r"\]\(([^\s)]+)\)", text):
                if ":" not in reference and not reference.startswith("#"):
                    target = str(PurePosixPath(path).parent / reference.split("#")[0])
                    if target not in draft.files:
                        warnings.append(f"{path}: 引用文件 {reference} 不存在")
    if config.get("required_tools"):
        warnings.append("必需外部工具将在 Agent 关联及运行提交时核对，不从 Skill 自动授予权限")
    return {
        "revision": draft.revision,
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "content_hash": digest,
        "config": config,
        "scripts": script_statuses,
    }


def script_fields(path: str, spec: dict, texts: dict) -> dict:
    """将包内声明转换为既有只读沙箱 Tool 的执行定义。"""
    from app.core.enums import ScriptLanguage, ToolKind, ToolEffect, RecoveryPolicy
    from app.services.user_tools import validate_tool_definition, version_fields

    if package_path(path) not in texts or not path.startswith("scripts/") or not isinstance(spec, dict):
        raise ValueError("脚本必须引用 scripts/ 中的 UTF-8 文件")
    if set(spec) - {"description", "input_schema", "requirements", "requires_approval"}:
        raise ValueError("脚本仅支持 description、input_schema、requirements、requires_approval；沙箱禁止网络和外部写入")
    suffix = PurePosixPath(path).suffix
    if suffix not in {".py", ".sh"}:
        raise ValueError("只支持 Python 或 Bash 脚本")
    body = {
        "visibility": Visibility.PRIVATE,
        "requirements": spec.get("requirements", []),
        "input_schema": spec.get("input_schema"),
        "source_code": texts[path],
        "script_language": ScriptLanguage.PYTHON if suffix == ".py" else ScriptLanguage.BASH,
        "recovery_policy": RecoveryPolicy.SAFE,
    }
    if not isinstance(spec.get("requires_approval", False), bool) or not isinstance(spec.get("description", ""), str):
        raise ValueError("脚本描述须为文字，requires_approval 须为布尔值")
    fields = version_fields(body)
    validate_tool_definition(ToolKind.SCRIPT, "skill_script", fields)
    fields.pop("visibility", None)
    fields["effect"] = ToolEffect.READ
    fields["requires_approval"] = bool(spec.get("requires_approval", False))
    return fields


def unpack_package(data: bytes) -> dict[str, bytes]:
    """有界读取单根 ZIP，拒绝目录逃逸、链接、重复文件和压缩炸弹。"""
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError("ZIP 最多 32 MiB")
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_PACKAGE_FILES * 2 or sum(item.file_size for item in entries) > MAX_PACKAGE_BYTES:
            raise ValueError("ZIP 文件数或解压总量超过限制")
        names = [item.filename for item in entries if not item.is_dir()]
        prefix = ""
        if "SKILL.md" not in names:
            roots = {name.split("/")[0] for name in names}
            if len(roots) != 1:
                raise ValueError("ZIP 必须包含一个 Skill 根目录和 SKILL.md")
            prefix = next(iter(roots)) + "/"
            if prefix + "SKILL.md" not in names:
                raise ValueError("ZIP 根目录缺少 SKILL.md")
        for item in entries:
            if stat.S_IFMT(item.external_attr >> 16) not in {0, stat.S_IFREG, stat.S_IFDIR} or item.flag_bits & 1:
                raise ValueError("ZIP 不支持加密、符号链接或特殊文件")
            package_path(item.filename.rstrip("/"))
            if item.is_dir():
                continue
            path = package_path(item.filename.removeprefix(prefix))
            if path in result or item.file_size > MAX_RESOURCE_BYTES:
                raise ValueError("ZIP 包含重复路径或超大文件")
            result[path] = archive.read(item)
    check_entries(
        {
            path: {
                "bytes": len(raw),
            }
            for path, raw in result.items()
        }
    )
    return result


async def check_quota(db: AsyncSession, owner_id: int, saved: list) -> None:
    """在新资源尚未 flush 前复用用户配额检查，避免重复计数。"""
    with db.no_autoflush:
        await enforce_owner_quota(db, owner_id, add_count=len(saved), add_bytes=sum(item.byte_size for item, _ in saved))


async def replace_draft(db: AsyncSession, draft: SkillDraft, files: dict, expected_revision: int, settings: dict | None = None) -> None:
    """通过比较并交换一次性写入完整草稿，拒绝过期编辑。"""
    check_entries(files)
    if "SKILL.md" not in files:
        raise ValueError("不能删除 SKILL.md")
    values = {
        "files": files,
        "revision": expected_revision + 1,
    }
    if settings is not None:
        values["settings"] = settings
    result = await db.execute(
        update(SkillDraft)
        .where(SkillDraft.id == draft.id, SkillDraft.revision == expected_revision)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise CustomException(status_code=409, custom_code=10005, detail="Skill 草稿已变化，请刷新后比较修改")
    retained = {entry["artifact_id"] for entry in files.values()}
    removed = {entry["artifact_id"] for entry in draft.files.values()} - retained
    for artifact_id in removed:
        published = await db.scalar(select(SkillVersionFile.id).where(SkillVersionFile.artifact_id == artifact_id).limit(1))
        template = await db.scalar(select(SkillTemplate.id).where(SkillTemplate.artifact_id == artifact_id).limit(1))
        if published is None and template is None:
            resource = await db.get(Artifact, artifact_id)
            if resource is not None:
                # 未发布的被替换草稿资源进入既有过期清理，历史发布资源继续保留。
                resource.expires_at = shanghai_now(naive=True)
