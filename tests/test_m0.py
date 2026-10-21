import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.models.base import Base


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_health() -> None:
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_initial_migration(tmp_path: Path) -> None:
    database = tmp_path / "platform.db"
    env = os.environ.copy()
    env.update({"DB_BACKEND": "sqlite", "SQLITE_DATABASE": str(database)})
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"alembic_version", *Base.metadata.tables} <= tables
        for table in Base.metadata.sorted_tables:
            columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table.name}")')}
            assert {column.name for column in table.columns} == columns
