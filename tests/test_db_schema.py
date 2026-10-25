"""数据库启动检查应在请求写入前发现尚未应用的迁移。"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.session import check_mapped_schema
from app.models.base import Base


def test_startup_rejects_missing_artifact_column(tmp_path) -> None:
    async def exercise() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old-schema.db'}")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
                await connection.execute(text("ALTER TABLE yp_artifact DROP COLUMN source_artifact_id"))
                with pytest.raises(RuntimeError, match="yp_artifact.source_artifact_id"):
                    await connection.run_sync(check_mapped_schema)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
