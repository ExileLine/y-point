# -*- coding: utf-8 -*-
# @Time    : 2026-10-21 08:52:04
# @Author  : yangyuexiong
# @File    : test_m0.py

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models.base import Base
from app.core import config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_health() -> None:
    """验证基础健康检查接口可访问。"""
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"code": 200, "message": "操作成功", "data": {"status": "ok"}}


def test_initial_migration(tmp_path: Path) -> None:
    """验证迁移后的表字段与模型一致且不包含外键约束。"""
    database = tmp_path / "platform.db"
    env = os.environ.copy()
    env.update({"DB_BACKEND": "sqlite", "SQLITE_DATABASE": str(database)})
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=PROJECT_ROOT, env=env, check=True, capture_output=True, text=True
    )
    subprocess.run([sys.executable, "-m", "alembic", "check"], cwd=PROJECT_ROOT, env=env, check=True, capture_output=True, text=True)
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"alembic_version", *Base.metadata.tables} <= tables
        for table in Base.metadata.sorted_tables:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table.name}")')}
            assert {column.name for column in table.columns} == columns
            assert connection.execute(f'PRAGMA foreign_key_list("{table.name}")').fetchall() == []
            assert table.comment and all(column.comment for column in table.columns)


def test_development_local_secret_overrides_example(tmp_path: Path, monkeypatch) -> None:
    """验证未跟踪的开发配置可覆盖仓库内的占位密钥。"""
    (tmp_path / ".env.development").write_text("SECRET_KEY=your-secret-key\n", encoding="utf-8")
    (tmp_path / ".env.development.local").write_text("SECRET_KEY=local-testing-secret-with-more-than-32-bytes\n", encoding="utf-8")
    monkeypatch.setattr(config, "BASE_DIR", tmp_path)
    monkeypatch.delenv("ENV_FILE", raising=False)
    files = config.resolve_env_files("development")
    settings = config.BaseConfig(_env_file=[str(path) for path in files])
    assert settings.SECRET_KEY == "local-testing-secret-with-more-than-32-bytes"
