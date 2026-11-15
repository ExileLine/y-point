# -*- coding: utf-8 -*-
# @Time    : 2026-11-13 15:21:14
# @Author  : yangyuexiong
# @File    : test_deployment.py

"""验证部署初始化的凭据稳定性、环境隔离与 Compose 合并后的权限边界。"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def initialize(directory: Path, *arguments: str) -> subprocess.CompletedProcess:
    """通过真实 CLI 在临时目录生成独立的部署配置。"""
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/deploy.py"), "init", "--directory", str(directory), *arguments],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_deployment_initialization_preserves_secrets_and_does_not_repair_partial_configuration(tmp_path: Path) -> None:
    """HTTP 默认不要求证书；重复初始化保留凭据，缺损配置不得偷偷轮换密钥。"""
    directory = tmp_path / "private"
    first = initialize(directory)
    assert first.returncode == 0, first.stderr
    files = [directory / name for name in ("application.env", "database.env", "compose.env", "redis.env")]
    assert not (directory / "tls").exists()
    original = [path.read_bytes() for path in files]
    for path in files:
        assert path.stat().st_mode & 0o077 == 0
    second = initialize(directory)
    assert second.returncode == 0, second.stderr
    assert [path.read_bytes() for path in files] == original
    application = original[0].decode()
    assert "STREAM_COOKIE_SECURE=False" in application
    assert "DEBUG=False" in application and "WORKER_LOG_STDOUT=True" in application
    assert str(directory / "data/tool-sandbox") in application
    assert (directory / "data/runtime/package_proxy.py").read_bytes() == (ROOT / "sandbox/package_proxy.py").read_bytes()
    wrong_environment = initialize(directory, "--environment", "production")
    assert wrong_environment.returncode != 0
    assert [path.read_bytes() for path in files] == original
    (directory / "database.env").unlink()
    damaged = initialize(directory)
    assert damaged.returncode != 0
    assert (directory / "application.env").read_bytes() == original[0]
    assert not (directory / "database.env").exists()


def test_production_initialization_requires_explicit_certificate(tmp_path: Path) -> None:
    """正式环境在没有证书时拒绝初始化，避免生成可被误用的本机证书。"""
    directory = tmp_path / "production"
    result = initialize(directory, "--environment", "production", "--https")
    assert result.returncode != 0
    assert not directory.exists()


@pytest.mark.skipif(shutil.which("docker") is None or shutil.which("openssl") is None, reason="需要 Docker Compose 与 openssl")
@pytest.mark.parametrize("https", [False, True])
def test_compose_sandbox_is_explicit_and_keeps_server_configuration(tmp_path: Path, https: bool) -> None:
    """覆盖配置只给受信任服务增加 Docker 权限，同时保留密钥挂载和生产环境。"""
    directory = tmp_path / "private"
    result = initialize(directory, *(["--https"] if https else []))
    assert result.returncode == 0, result.stderr
    command = [sys.executable, str(ROOT / "scripts/deploy.py"), "compose", "--directory", str(directory)]
    for sandbox in (False, True):
        completed = subprocess.run(
            [*command, *(["--sandbox"] if sandbox else []), "--", "config", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        config = json.loads(completed.stdout)
        for service in ("api", "worker"):
            server = config["services"][service]
            assert server["environment"]["FAST_API_ENV"] == "production"
            assert server["environment"]["ENV_FILE"] == "/run/config/application.env"
            assert server["environment"]["WORKER_LOG_STDOUT"] == "True"
            assert server["environment"]["STREAM_COOKIE_SECURE"] == str(https)
            assert server["read_only"] and server["cap_drop"] == ["ALL"]
            mounts = {volume["target"] for volume in server["volumes"]}
            assert "/run/config/application.env" in mounts
            assert str(directory / "data") in mounts
            assert ("/var/run/docker.sock" in mounts) == sandbox
            assert "ports" not in server
            assert server["logging"]["driver"] == "local"
        web = config["services"]["web"]
        assert web["environment"]["YPOINT_HTTPS_ENABLED"] == str(https).lower()
        assert {port["target"] for port in web["ports"]} == ({8080, 8443} if https else {8080})
        assert any(volume["target"] == "/run/tls" for volume in web.get("volumes", [])) == https
        assert "ports" not in config["services"]["db"]
        redis = config["services"]["redis"]
        assert "ports" not in redis and redis["user"] == "redis"
        assert redis["read_only"] and redis["logging"]["driver"] == "local"
        assert redis["environment"]["REDISCLI_AUTH"] == config["services"]["api"]["environment"]["REDIS_PWD"]
        assert "SECRET_KEY" not in redis["environment"] and "POSTGRES_PASSWORD" not in redis["environment"]
        assert "REDISCLI_AUTH" not in " ".join(redis["command"])
        assert "redis-cli --raw ping" in redis["healthcheck"]["test"][-1]
        assert any(volume["type"] == "volume" and volume["target"] == "/data" for volume in redis["volumes"])
        for service in ("api", "worker"):
            assert config["services"][service]["environment"]["REDIS_ENABLED"] == "True"
            assert config["services"][service]["environment"]["REDIS_HOST"] == "redis"
            assert config["services"][service]["depends_on"]["redis"]["condition"] == "service_healthy"
        assert config["services"]["api"]["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_http_initialization_needs_neither_certificate_nor_openssl(tmp_path: Path, environment: str) -> None:
    """各部署环境均可在没有 OpenSSL 的情况下初始化 HTTP，不生成证书目录。"""
    directory = tmp_path / environment
    environment_variables = os.environ.copy()
    environment_variables["PATH"] = ""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/deploy.py"), "init", "--directory", str(directory), "--environment", environment],
        env=environment_variables,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not (directory / "tls").exists()
    assert "YPOINT_HTTPS_ENABLED='false'" in (directory / "compose.env").read_text()
    assert "STREAM_COOKIE_SECURE=False" in (directory / "application.env").read_text()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="HTTPS 测试需要 OpenSSL")
def test_protocol_switch_and_legacy_configuration_preserve_credentials(tmp_path: Path) -> None:
    """协议可双向切换；旧配置默认继续 HTTPS，切换不改密码、业务密钥和证书。"""
    from scripts.deploy import configured_https, read_private_values

    directory = tmp_path / "switch"
    assert initialize(directory).returncode == 0
    before = read_private_values(directory / "application.env")
    database = (directory / "database.env").read_bytes()
    enabled = initialize(directory, "--https")
    assert enabled.returncode == 0, enabled.stderr
    assert configured_https(read_private_values(directory / "compose.env"))
    assert read_private_values(directory / "application.env")["STREAM_COOKIE_SECURE"] == "True"
    certificates = [(directory / "tls" / name).read_bytes() for name in ("tls.crt", "tls.key")]
    repeated = initialize(directory)
    assert repeated.returncode == 0, repeated.stderr
    assert certificates == [(directory / "tls" / name).read_bytes() for name in ("tls.crt", "tls.key")]
    for name in ("compose.env", "application.env"):
        target = directory / name
        target.write_text(
            "\n".join(line for line in target.read_text().splitlines() if not line.startswith(("YPOINT_HTTPS_ENABLED=", "STREAM_COOKIE_SECURE=")))
        )
    assert configured_https(read_private_values(directory / "compose.env"))
    migrated = initialize(directory)
    assert migrated.returncode == 0, migrated.stderr
    assert read_private_values(directory / "application.env")["STREAM_COOKIE_SECURE"] == "True"
    # 关闭 HTTPS 不要求旧证书仍存在。
    (directory / "tls/tls.crt").unlink()
    disabled = initialize(directory, "--http")
    assert disabled.returncode == 0, disabled.stderr
    assert not configured_https(read_private_values(directory / "compose.env"))
    assert read_private_values(directory / "application.env") == before
    assert (directory / "database.env").read_bytes() == database
    assert (directory / "tls/tls.key").read_bytes() == certificates[1]


@pytest.mark.skipif(shutil.which("openssl") is None, reason="准备测试证书需要 OpenSSL")
def test_certificate_arguments_enable_https_and_invalid_combinations_fail(tmp_path: Path) -> None:
    """兼容旧版提供证书即启用 HTTPS；拒绝证书缺项和 HTTP/证书冲突。"""
    source = tmp_path / "source"
    assert initialize(source, "--https").returncode == 0
    certificate_arguments = ["--tls-cert", str(source / "tls/tls.crt"), "--tls-key", str(source / "tls/tls.key")]
    directory = tmp_path / "production"
    enabled = initialize(directory, "--environment", "production", *certificate_arguments)
    assert enabled.returncode == 0, enabled.stderr
    assert "YPOINT_HTTPS_ENABLED='true'" in (directory / "compose.env").read_text()
    assert (directory / "tls/tls.crt").read_bytes() == (source / "tls/tls.crt").read_bytes()
    for arguments in (("--http", *certificate_arguments), ("--tls-cert", str(source / "tls/tls.crt"))):
        invalid = tmp_path / "invalid"
        assert initialize(invalid, *arguments).returncode != 0
        assert not invalid.exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 Docker Compose")
def test_production_http_keeps_resource_overlay_without_tls(tmp_path: Path) -> None:
    """生产资源覆盖与 HTTP 协议独立，不挂载或发布 HTTPS 资源。"""
    directory = tmp_path / "production"
    assert initialize(directory, "--environment", "production").returncode == 0
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/deploy.py"),
            "compose",
            "--environment",
            "production",
            "--directory",
            str(directory),
            "--",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert int(services["api"]["mem_limit"]) == 4 * 1024**3
    assert int(services["db"]["mem_limit"]) == 2 * 1024**3
    assert [port["target"] for port in services["web"]["ports"]] == [8080]
    assert not services["web"].get("volumes")


def test_reinitialization_preserves_external_gateway_cookie_setting(tmp_path: Path) -> None:
    """外层网关终止 TLS 时，无协议参数的重复初始化保留显式 Cookie 设置。"""
    from scripts.deploy import read_private_values, update_private_values

    directory = tmp_path / "external-gateway"
    assert initialize(directory).returncode == 0
    update_private_values(directory / "application.env", {"STREAM_COOKIE_SECURE": "True"})
    assert initialize(directory).returncode == 0
    assert read_private_values(directory / "application.env")["STREAM_COOKIE_SECURE"] == "True"
    assert initialize(directory, "--http").returncode == 0
    assert read_private_values(directory / "application.env")["STREAM_COOKIE_SECURE"] == "False"


def test_http_port_can_match_unused_https_port(tmp_path: Path) -> None:
    """HTTP 不占用 HTTPS 端口；切换为 HTTPS 前必须拒绝保存的端口冲突。"""
    directory = tmp_path / "ports"
    result = initialize(directory, "--http-port", "18443")
    assert result.returncode == 0, result.stderr
    before = (directory / "compose.env").read_bytes()
    assert initialize(directory, "--https").returncode != 0
    assert (directory / "compose.env").read_bytes() == before
    assert not (directory / "tls").exists()


@pytest.mark.skipif(shutil.which("docker") is None or shutil.which("openssl") is None, reason="需要 Docker Compose 与 OpenSSL")
def test_legacy_compose_configuration_keeps_https_overlay(tmp_path: Path) -> None:
    """旧配置即使没有协议开关，compose 仍合并证书挂载和 HTTPS 端口。"""
    directory = tmp_path / "legacy"
    assert initialize(directory, "--https").returncode == 0
    path = directory / "compose.env"
    path.write_text("\n".join(line for line in path.read_text().splitlines() if not line.startswith("YPOINT_HTTPS_ENABLED=")) + "\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/deploy.py"), "compose", "--directory", str(directory), "--", "config", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    web = json.loads(result.stdout)["services"]["web"]
    assert web["environment"]["YPOINT_HTTPS_ENABLED"] == "true"
    assert {port["target"] for port in web["ports"]} == {8080, 8443}
    assert any(volume["target"] == "/run/tls" for volume in web["volumes"])


def test_explicit_listening_updates_preserve_other_configuration(tmp_path: Path) -> None:
    """可更新已有部署的监听参数，省略时保留且不改业务凭据和镜像标签。"""
    from scripts.deploy import read_private_values, update_private_values

    directory = tmp_path / "listening"
    assert initialize(directory, "--bind-ip", "0.0.0.0", "--http-port", "19080").returncode == 0
    config = directory / "compose.env"
    update_private_values(
        config,
        {
            "YPOINT_SERVER_IMAGE": "'custom-server:release'",
        },
    )
    credentials = [(directory / name).read_bytes() for name in ("application.env", "database.env")]
    assert initialize(directory).returncode == 0
    preserved = read_private_values(config)
    assert preserved["YPOINT_BIND_IP"] == "0.0.0.0" and preserved["YPOINT_HTTP_PORT"] == "19080"
    updated = initialize(directory, "--bind-ip", "192.168.1.10", "--http-port", "19081", "--https-port", "19443")
    assert updated.returncode == 0, updated.stderr
    values = read_private_values(config)
    assert values["YPOINT_BIND_IP"] == "192.168.1.10"
    assert values["YPOINT_HTTP_PORT"] == "19081" and values["YPOINT_HTTPS_PORT"] == "19443"
    assert values["YPOINT_SERVER_IMAGE"] == "custom-server:release"
    assert credentials == [(directory / name).read_bytes() for name in ("application.env", "database.env")]
    before = config.read_bytes()
    assert initialize(directory, "--http-port", "0").returncode != 0
    assert config.read_bytes() == before
    invalid = tmp_path / "invalid"
    assert initialize(invalid, "--bind-ip", "invalid-host").returncode != 0
    assert not invalid.exists()


def test_worker_probe_rejects_own_stale_heartbeat_even_with_another_live_worker(tmp_path: Path) -> None:
    """健康探针必须匹配自身 UUID；其他活跃 worker 不能掩盖本进程心跳失联。"""
    import asyncio
    import os
    from datetime import timedelta

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models.base import shanghai_now
    from app.models.run_worker_heartbeat import RunWorkerHeartbeat

    database = tmp_path / "heartbeats.sqlite"
    marker = tmp_path / "worker-id"
    marker.write_text("this-worker", encoding="utf-8")

    async def prepare() -> None:
        """准备一条陈旧心跳和一条活跃心跳，保留最小真实数据库协议。"""
        engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as connection:
            await connection.run_sync(RunWorkerHeartbeat.__table__.create)
        async with sessions.begin() as db:
            now = shanghai_now(naive=True)
            db.add(RunWorkerHeartbeat(id="this-worker", started_at=now, last_seen_at=now - timedelta(seconds=90)))
            db.add(RunWorkerHeartbeat(id="other-worker", started_at=now, last_seen_at=now))
        await engine.dispose()

    asyncio.run(prepare())
    environment = os.environ.copy()
    environment.update(
        {
            "DB_BACKEND": "sqlite",
            "SQLITE_DATABASE": str(database),
            "WORKER_HEALTH_FILE": str(marker),
        },
    )
    command = [sys.executable, str(ROOT / "deploy/healthcheck.py"), "worker"]
    stale = subprocess.run(command, env=environment, capture_output=True, timeout=15)
    assert stale.returncode == 1
    marker.write_text("other-worker", encoding="utf-8")
    live = subprocess.run(command, env=environment, capture_output=True, timeout=15)
    assert live.returncode == 0


@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_redis_password_is_independent_private_and_stable(tmp_path: Path, environment: str) -> None:
    """各环境启用托管 Redis，密码独立且重复初始化不会轮换或泄露。"""
    from scripts.deploy import read_private_values

    directory = tmp_path / environment
    first = initialize(directory, "--environment", environment)
    assert first.returncode == 0, first.stderr
    application = read_private_values(directory / "application.env")
    redis = read_private_values(directory / "redis.env")
    assert application["REDIS_ENABLED"] == "True" and application["REDIS_HOST"] == "redis"
    assert application["REDIS_PORT"] == "6379" and application["REDIS_DB"] == "0"
    assert application["REDIS_PWD"] == redis["REDISCLI_AUTH"]
    assert application["REDIS_PWD"] != application["POSTGRESQL_PASSWORD"]
    assert application["REDIS_PWD"] not in first.stdout + first.stderr
    assert (directory / "redis.env").stat().st_mode & 0o077 == 0
    before = {path.name: path.read_bytes() for path in directory.glob("*.env")}
    second = initialize(directory, "--environment", environment)
    assert second.returncode == 0, second.stderr
    assert before == {path.name: path.read_bytes() for path in directory.glob("*.env")}


def test_legacy_deployment_adds_redis_without_rotating_business_credentials(tmp_path: Path) -> None:
    """旧三文件部署补齐 Redis，已有数据库、JWT 和供应商密钥及镜像配置均保留。"""
    from scripts.deploy import read_private_values

    directory = tmp_path / "legacy-redis"
    assert initialize(directory).returncode == 0
    compose_path = directory / "compose.env"
    compose_path.write_text("\n".join(line for line in compose_path.read_text().splitlines() if not line.startswith("YPOINT_REDIS_")) + "\n")
    application_path = directory / "application.env"
    application_path.write_text(
        "\n".join(line for line in application_path.read_text().splitlines() if not line.startswith("REDIS_")) + "\nREDIS_ENABLED=False\n",
    )
    (directory / "redis.env").unlink()
    before = read_private_values(application_path)
    database = (directory / "database.env").read_bytes()
    result = initialize(directory)
    assert result.returncode == 0, result.stderr
    after = read_private_values(application_path)
    assert after["REDIS_ENABLED"] == "True" and after["REDIS_HOST"] == "redis"
    assert {key: value for key, value in after.items() if not key.startswith("REDIS_")} == {
        key: value for key, value in before.items() if not key.startswith("REDIS_")
    }
    assert (directory / "database.env").read_bytes() == database
    assert after["REDIS_PWD"] == read_private_values(directory / "redis.env")["REDISCLI_AUTH"]
    assert read_private_values(compose_path)["YPOINT_REDIS_ENV"] == str(directory / "redis.env")


@pytest.mark.parametrize("damage", ["missing", "empty", "mismatch"])
def test_damaged_managed_redis_configuration_is_not_regenerated(tmp_path: Path, damage: str) -> None:
    """托管 Redis 凭据缺失、为空或不匹配时拒绝修改配置，防止隐式密码轮换。"""
    directory = tmp_path / "damaged-redis"
    assert initialize(directory).returncode == 0
    path = directory / "redis.env"
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_text("REDISCLI_AUTH=\n")
    else:
        path.write_text("REDISCLI_AUTH=different-password\n")
    before = {file.name: file.read_bytes() for file in directory.glob("*.env")}
    result = initialize(directory)
    assert result.returncode != 0
    assert before == {file.name: file.read_bytes() for file in directory.glob("*.env")}
    assert "Redis" in result.stderr


def test_legacy_external_redis_configuration_is_preserved(tmp_path: Path) -> None:
    """旧部署已指定外部 Redis 时，仅补容器私有配置，保留原连接参数。"""
    from scripts.deploy import read_private_values, update_private_values

    directory = tmp_path / "external-redis"
    assert initialize(directory).returncode == 0
    compose = directory / "compose.env"
    compose.write_text("\n".join(line for line in compose.read_text().splitlines() if not line.startswith("YPOINT_REDIS_")) + "\n")
    (directory / "redis.env").unlink()
    update_private_values(
        directory / "application.env",
        {
            "REDIS_HOST": "redis.internal",
            "REDIS_PWD": "existing-external-password",
            "REDIS_PORT": "6380",
            "REDIS_DB": "3",
        },
    )
    before = (directory / "application.env").read_bytes()
    result = initialize(directory)
    assert result.returncode == 0, result.stderr
    assert (directory / "application.env").read_bytes() == before
    assert read_private_values(directory / "redis.env")["REDISCLI_AUTH"] != "existing-external-password"


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 Docker Compose")
@pytest.mark.parametrize("environment", ["local", "staging", "production"])
def test_redis_resource_defaults_reserve_memory_for_persistence(tmp_path: Path, environment: str) -> None:
    """Compose 环境覆盖同时调整容器上限与数据内存，为 AOF 和客户端缓冲预留空间。"""
    directory = tmp_path / environment
    assert initialize(directory, "--environment", environment).returncode == 0
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/deploy.py"),
            "compose",
            "--environment",
            environment,
            "--directory",
            str(directory),
            "--",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    redis = json.loads(result.stdout)["services"]["redis"]
    production = environment == "production"
    assert int(redis["mem_limit"]) == (1024 if production else 512) * 1024 * 1024
    assert redis["environment"]["REDIS_MAXMEMORY"] == ("512mb" if production else "256mb")


def test_orphaned_redis_credentials_do_not_trigger_new_database_keys(tmp_path: Path) -> None:
    """只剩 Redis 私有文件时拒绝初始化，避免部分恢复被误认为全新部署。"""
    directory = tmp_path / "orphaned"
    directory.mkdir()
    path = directory / "redis.env"
    path.write_text("REDISCLI_AUTH=preserved-password\n")
    result = initialize(directory)
    assert result.returncode != 0
    assert path.read_text() == "REDISCLI_AUTH=preserved-password\n"
    assert not (directory / "application.env").exists()
    assert not (directory / "database.env").exists()
