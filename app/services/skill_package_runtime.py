# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:14:37
# @Author  : yangyuexiong
# @File    : skill_package_runtime.py

"""Skill 包资源与现有 Agent、Tool 和 Run 执行链路的授权边界。"""

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.custom_exception import CustomException
from app.core.enums import Visibility
from app.models.skill import AgentSkill, RunSkill, Skill, SkillVersion
from app.models.skill_package import SkillScript
from app.models.tool import AgentTool, RunTool, UserTool, UserToolVersion
from app.services.skill_packages import execution_config, read_resource
from app.services.user_tools import model_tool_name


async def require_dependencies(db: AsyncSession, versions: list[SkillVersion], tools: list[UserTool]) -> None:
    """仅接受显式关联且名称唯一的外部工具，Skill 配置不扩展工具授权。"""
    names = [tool.name for tool in tools]
    for version in versions:
        config = execution_config(version.files) if any(entry.get("package_file") for entry in version.manifest) else {}
        missing = [name for name in config.get("required_tools", []) if names.count(name) != 1]
        if missing:
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 必需工具未关联、不可用或重名：" + "、".join(missing))


async def check_agent_dependencies(db: AsyncSession, skills: list, tools: list[UserTool]) -> None:
    """在 Agent 保存前核对所选版本依赖，同时保留已有 Agent 的固定版本。"""
    versions = []
    for item in skills:
        skill_id = item.skill_id if isinstance(item, AgentSkill) else item.id
        number = item.skill_version if isinstance(item, AgentSkill) else item.current_version
        version = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill_id, SkillVersion.version == number))
        if version is None:
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 发布版本不可用")
        versions.append(version)
    await require_dependencies(db, versions, tools)


async def snapshot_skill_scripts(db: AsyncSession, run_id: str, agent_id: int, actor_id: int, skills: list[AgentSkill]) -> None:
    """在创建 Run 的事务中固定包内脚本，检查本轮外部依赖实际可调用。"""
    tools = list(
        await db.scalars(
            select(UserTool)
            .join(AgentTool, AgentTool.tool_id == UserTool.id)
            .where(
                AgentTool.agent_id == agent_id,
                UserTool.is_deleted == 0,
                UserTool.enabled.is_(True),
                UserTool.skill_id.is_(None),
                (UserTool.owner_id == actor_id) | (UserTool.visibility == Visibility.PUBLIC),
            ),
        ),
    )
    verified_tools = []
    for tool in tools:
        link = await db.scalar(select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id))
        fixed = await db.scalar(select(UserToolVersion).where(UserToolVersion.tool_id == tool.id, UserToolVersion.version == link.tool_version))
        if fixed is not None and fixed.verified:
            from app.services.user_tools import _credentials_shared

            if fixed.encrypted_credentials and (
                fixed.version <= (tool.credentials_revoked_through or 0) or tool.owner_id != actor_id and not await _credentials_shared(db, tool)
            ):
                continue
            verified_tools.append(tool)
    versions = []
    for row in skills:
        version = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == row.skill_id, SkillVersion.version == row.skill_version))
        if version is None:
            raise CustomException(status_code=409, custom_code=10005, detail="Skill 发布版本不可用")
        versions.append(version)
        bindings = list(
            await db.scalars(
                select(SkillScript).where(
                    SkillScript.skill_id == row.skill_id,
                    SkillScript.package_hash == version.content_hash,
                ),
            ),
        )
        for binding in bindings:
            fixed = await db.scalar(
                select(UserToolVersion).where(
                    UserToolVersion.tool_id == binding.tool_id,
                    UserToolVersion.version == binding.tool_version,
                ),
            )
            if fixed is None or not fixed.verified or fixed.content_hash != version.content_hash:
                raise CustomException(status_code=409, custom_code=10005, detail="Skill 包内脚本尚未验证")
            db.add(RunTool(run_id=run_id, tool_id=binding.tool_id, tool_version=binding.tool_version, content_hash=fixed.content_hash))
    await require_dependencies(db, versions, verified_tools)


async def managed_script_resources(
    db: AsyncSession,
    run_id: str,
    actor_id: int,
    tool: UserTool,
    version: UserToolVersion,
    *,
    read_files: bool = True,
) -> dict:
    """复核 Skill 当前授权和 Run 固定包，只读取该包的资源与脚本入口。"""
    skill = await db.get(Skill, tool.skill_id)
    row = await db.scalar(select(RunSkill).where(RunSkill.run_id == run_id, RunSkill.skill_id == tool.skill_id))
    if skill is None or skill.is_deleted or not skill.enabled or (skill.owner_id != actor_id and skill.visibility != Visibility.PUBLIC) or row is None:
        raise CustomException(status_code=409, custom_code=10005, detail="Skill 脚本授权已失效")
    fixed = await db.scalar(select(SkillVersion).where(SkillVersion.skill_id == skill.id, SkillVersion.version == row.skill_version))
    binding = await db.scalar(
        select(SkillScript).where(
            SkillScript.skill_id == skill.id,
            SkillScript.tool_id == tool.id,
            SkillScript.tool_version == version.version,
            SkillScript.package_hash == version.content_hash,
        ),
    )
    if fixed is None or fixed.content_hash != version.content_hash or binding is None:
        raise CustomException(status_code=409, custom_code=10005, detail="Skill 脚本与固定包版本不一致")
    if not read_files:
        return {}
    entries = {entry["path"]: entry for entry in fixed.manifest if entry.get("package_file")}
    resources = {path: await read_resource(db, skill.owner_id, entry) for path, entry in entries.items()}
    return {
        "package_files": resources,
        "entrypoint": binding.path,
    }


async def package_instructions(db: AsyncSession, skill: Skill, version: SkillVersion) -> str:
    """附加受控资源目录和精确脚本工具映射，参考正文按需读取。"""
    entries = [entry["path"] for entry in version.manifest if entry.get("package_file")]
    bindings = list(await db.scalars(select(SkillScript).where(SkillScript.skill_id == skill.id, SkillScript.package_hash == version.content_hash)))
    scripts = {}
    for binding in bindings:
        tool = await db.get(UserTool, binding.tool_id)
        if tool is not None:
            scripts[binding.path] = model_tool_name(tool)
    return (
        "\n\n## 本发布版本的文件包资源\n"
        f"虚拟根目录：/skills/{skill.name}/。参考资料可通过 read_file 按需读取；二进制模板由 render_skill_document 使用完整路径生成产物。\n"
        "包内脚本通过以下已授权工具调用，参数遵循工具 Schema。脚本只读挂载此固定包，无网络，JSON 输入输出；"
        "需落盘的报告请将脚本结果交给模板生成工具，不执行宿主机路径。\n"
        + json.dumps(
            {
                "files": entries,
                "scripts": scripts,
            },
            ensure_ascii=False,
        )
    )
