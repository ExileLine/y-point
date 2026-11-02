# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 10:33:31
# @Author  : yangyuexiong
# @File    : skills.py

"""说明型 Skill 包校验、版本固定和运行授权。"""

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import PurePosixPath

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.models.skill import AgentSkill, RunSkill, Skill, SkillVersion

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_FILES = 32
MAX_FILE_BYTES = 128 * 1024
MAX_TOTAL_BYTES = 512 * 1024
MAX_ZIP_BYTES = 1024 * 1024
TEMPLATE = "---\nname: example-skill\ndescription: 简要说明何时使用这个 Skill\n---\n\n# 操作说明\n\n写明步骤、约束和预期输出。\n"


def validate_package(files: dict[str, str]) -> tuple[str, str, list[dict], str]:
    """校验路径、纯文本清单及标准 SKILL.md 元数据。"""
    if not files or len(files) > MAX_FILES or "SKILL.md" not in files:
        raise ValueError("Skill 必须包含 SKILL.md，且最多 32 个文件")
    normalized = {}
    total = 0
    for path, content in files.items():
        pure = PurePosixPath(path)
        if (
            not path
            or len(path) > 256
            or any(len(part) > 128 for part in path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or "\\" in path
            or path.startswith("/")
            or pure.as_posix() != path
            or any(part in {".", ".."} for part in path.split("/"))
            or path.endswith("/")
            or "\x00" in path
        ):
            raise ValueError("Skill 文件路径不合法")
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("Skill 仅支持 UTF-8 文本文件")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("Skill 单文件超过 128 KiB")
        total += len(data)
        normalized[path] = data
    if total > MAX_TOTAL_BYTES:
        raise ValueError("Skill 包超过 512 KiB")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n", files["SKILL.md"], re.DOTALL)
    if match is None:
        raise ValueError("SKILL.md 必须以 YAML frontmatter 开头")
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError("SKILL.md 元数据不是有效 YAML") from exc
    if not isinstance(metadata, dict):
        raise ValueError("SKILL.md 元数据必须是对象")
    name, description = metadata.get("name"), metadata.get("description")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError("Skill 名称必须是小写字母、数字和连字符，最多 64 字符")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise ValueError("Skill 描述不能为空且最多 1024 字符")
    manifest = [
        {
            "path": path,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in sorted(normalized.items())
    ]
    digest = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return name, description.strip(), manifest, digest


def import_zip(data: bytes) -> dict[str, str]:
    """限制压缩包大小、路径和实际解压量后读取文本资源。"""
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("ZIP 超过 1 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            files = {}
            if len(entries) > MAX_FILES + 1 or sum(item.file_size for item in entries) > MAX_TOTAL_BYTES:
                raise ValueError("ZIP 文件数或解压量超过限制")
            for item in entries:
                if item.is_dir():
                    directory = item.filename.rstrip("/")
                    if (
                        not directory
                        or directory.startswith("/")
                        or "\\" in directory
                        or PurePosixPath(directory).as_posix() != directory
                        or any(part in {".", ".."} for part in directory.split("/"))
                    ):
                        raise ValueError("ZIP 目录路径不合法")
                    continue
                mode = item.external_attr >> 16
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                    raise ValueError("ZIP 不允许符号链接或特殊文件")
                if item.filename in files or item.file_size > MAX_FILE_BYTES:
                    raise ValueError("ZIP 包含重复文件或超大文件")
                raw = archive.read(item, pwd=None)
                if len(raw) != item.file_size:
                    raise ValueError("ZIP 文件长度不符")
                files[item.filename] = raw.decode("utf-8")
    except (zipfile.BadZipFile, UnicodeDecodeError, RuntimeError) as exc:
        raise ValueError("ZIP 无效或包含非 UTF-8 文件") from exc
    validate_package(files)
    return files


def export_zip(files: dict[str, str]) -> bytes:
    """将已经校验的 Skill 文件写入可移植 ZIP。"""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            archive.writestr(path, content)
    return output.getvalue()


async def selected_skills(db: AsyncSession, skill_ids: list[int], owner_id: int) -> list[Skill]:
    """校验当前用户能关联的启用 Skill，阻止同名包冲突。"""
    if len(skill_ids) != len(set(skill_ids)) or len(skill_ids) > 16:
        raise CustomException(status_code=400, custom_code=10005, detail="最多关联 16 个不重复的 Skill")
    skills = (await db.scalars(select(Skill).where(Skill.id.in_(skill_ids)).with_for_update())).all() if skill_ids else []
    if len(skills) != len(skill_ids) or any(
        item.is_deleted != 0 or not item.enabled or (item.owner_id != owner_id and item.visibility != "public") for item in skills
    ):
        raise CustomException(status_code=404, custom_code=10002, detail="Skill 不存在或不可用")
    if len({item.name for item in skills}) != len(skills):
        raise CustomException(status_code=409, custom_code=10005, detail="关联的 Skill 名称冲突")
    return skills


async def agent_skill_rows(db: AsyncSession, agent_id: int) -> list[AgentSkill]:
    """按关联次序读取 Agent 固定的 Skill 版本。"""
    return (await db.scalars(select(AgentSkill).where(AgentSkill.agent_id == agent_id).order_by(AgentSkill.id))).all()


async def snapshot_run_skills(db: AsyncSession, run_id: str, agent_id: int, owner_id: int) -> None:
    """在 Run 创建事务内固定关联版本并核对本次用户权限。"""
    rows = await agent_skill_rows(db, agent_id)
    selected = await selected_skills(db, [row.skill_id for row in rows], owner_id)
    by_id = {item.id: item for item in selected}
    if len({by_id[row.skill_id].name for row in rows}) != len(rows):
        raise CustomException(status_code=409, custom_code=10005, detail="Agent 的 Skill 名称冲突")
    db.add_all([RunSkill(run_id=run_id, skill_id=row.skill_id, skill_version=row.skill_version, is_deleted=0, status=1) for row in rows])


async def load_run_skill_files(db: AsyncSession, run_id: str, owner_id: int) -> dict[str, str]:
    """运行前复核已固定版本及当前授权，返回受控虚拟路径映射。"""
    rows = (await db.scalars(select(RunSkill).where(RunSkill.run_id == run_id).order_by(RunSkill.id))).all()
    skills = await selected_skills(db, [row.skill_id for row in rows], owner_id)
    by_id = {item.id: item for item in skills}
    files = {}
    for row in rows:
        version = await db.scalar(
            select(SkillVersion).where(SkillVersion.skill_id == row.skill_id, SkillVersion.version == row.skill_version)
        )
        if version is None:
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 版本不可用")
        for path, content in version.files.items():
            files[f"/skills/{by_id[row.skill_id].name}/{path}"] = content
    return files
