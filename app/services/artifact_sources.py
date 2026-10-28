# -*- coding: utf-8 -*-
# @Time    : 2026-10-28 09:09:07
# @Author  : yangyuexiong
# @File    : artifact_sources.py

"""批量读取生成产物在生成前成功读取的旧产物 ID。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.artifact_source import ArtifactSource


async def load_read_source_artifact_ids(db: AsyncSession, owner_id: int, target_ids: list[str]) -> dict[str, list[str]]:
    """按所有者批量加载生成产物的旧产物读取来源，并保留来源顺序。"""
    if not target_ids:
        return {}
    rows = (
        await db.execute(
            select(ArtifactSource.target_artifact_id, ArtifactSource.source_artifact_id)
            .where(ArtifactSource.target_artifact_id.in_(target_ids), ArtifactSource.owner_id == owner_id)
            .order_by(ArtifactSource.target_artifact_id, ArtifactSource.position)
        )
    ).all()
    by_target: dict[str, list[str]] = {}
    for target_id, source_id in rows:
        by_target.setdefault(target_id, []).append(source_id)
    return by_target
