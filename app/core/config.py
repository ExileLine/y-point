# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : config.py

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from loguru import logger
from urllib.parse import quote_plus
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


BASE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ENV = Environment.DEVELOPMENT.value
ENV_FILE_ENV_VAR = "ENV_FILE"
ENV_FILE_MAP = {
    Environment.DEVELOPMENT.value: ".env.development",
    Environment.TEST.value: ".env.test",
    Environment.STAGING.value: ".env.staging",
    Environment.PRODUCTION.value: ".env.production",
}


def normalize_env(env: Optional[str]) -> str:
    value = (env or "").strip().lower()
    mapping = {
        "dev": Environment.DEVELOPMENT.value,
        "development": Environment.DEVELOPMENT.value,
        "test": Environment.TEST.value,
        "testing": Environment.TEST.value,
        "stage": Environment.STAGING.value,
        "staging": Environment.STAGING.value,
        "prod": Environment.PRODUCTION.value,
        "production": Environment.PRODUCTION.value,
    }
    return mapping.get(value, value or DEFAULT_ENV)


def resolve_env_files(env_name: str) -> List[Path]:
    """按优先级装载环境配置，开发者本地配置最后覆盖。"""
    if env_name not in ENV_FILE_MAP:
        raise ValueError(f"Unsupported ENV: {env_name}")

    paths: List[Path] = []

    explicit = os.getenv(ENV_FILE_ENV_VAR)
    if explicit:
        explicit_path = Path(explicit)
        if not explicit_path.is_absolute():
            explicit_path = BASE_DIR / explicit_path
        if explicit_path.exists():
            paths.append(explicit_path)
        else:
            raise FileNotFoundError(f"{ENV_FILE_ENV_VAR} 指定的文件不存在: {explicit_path}")

    base_env = BASE_DIR / ".env"
    if base_env.exists():
        paths.append(base_env)

    env_specific = BASE_DIR / ENV_FILE_MAP[env_name]
    if env_specific.exists():
        paths.append(env_specific)

    if env_name == Environment.DEVELOPMENT.value:
        local_env = BASE_DIR / ".env.development.local"
        if local_env.exists():
            paths.append(local_env)

    if paths:
        return paths

    raise FileNotFoundError(
        f"未找到配置文件，请创建 {ENV_FILE_MAP.get(env_name)} 或设置 {ENV_FILE_ENV_VAR}"
    )


BASE_SETTINGS_CONFIG = SettingsConfigDict(
    env_file_encoding="utf-8",
    case_sensitive=False,
    extra="ignore",
)


class BaseConfig(BaseSettings):
    model_config = BASE_SETTINGS_CONFIG

    DOCS_TITLE: str = "y-point"
    DOCS_DESCRIPTION: str = "description"
    DOCS_SUMMARY: str = "I hope every day is fulfilling, and everything follows best practices."
    DOCS_VERSION: str = "1.0.0"
    DOCS_OPENAPI_URL: str = "/api/v1/openapi.json"
    DOCS_ENABLE: bool = True

    APP_NAME: str = "yangyuexiong"
    ENV: str = "development"
    DB_BACKEND: str = "sqlite"
    SECRET_KEY: str = "change-me"
    CREDENTIAL_ENCRYPTION_KEY: str = ""
    DEBUG: bool = True
    RUN_HOST: str = "0.0.0.0"
    RUN_PORT: int = 8000

    # 跨域配置：如果 allow_credentials=True，则 allow_origins 不能设置为 ["*"]，必须明确指定允许的域名。
    ALLOW_ORIGINS: list[str] = ["*"]
    ALLOW_CREDENTIALS: bool = False
    ALLOW_METHODS: list[str] = ["*"]
    ALLOW_HEADERS: list[str] = ["*"]

    MYSQL_HOSTNAME: str = "127.0.0.1"
    MYSQL_USERNAME: str = "root"
    MYSQL_PASSWORD: str = "password"
    MYSQL_PORT: int = 3306
    MYSQL_DATABASE: str = "y_point"

    POSTGRESQL_HOSTNAME: str = "127.0.0.1"
    POSTGRESQL_USERNAME: str = "postgres"
    POSTGRESQL_PASSWORD: str = "password"
    POSTGRESQL_PORT: int = 5432
    POSTGRESQL_DATABASE: str = "y_point"

    SQLITE_DATABASE: str = "y_point.db"
    CHECKPOINT_BACKEND: str = "sqlite"
    CHECKPOINT_POSTGRES_DSN: str = ""
    ARTIFACT_STORAGE_DIR: str = ".run-private/artifacts"
    ARTIFACT_RETENTION_DAYS: int = 30
    ARTIFACT_OWNER_MAX_BYTES: int = 1024 * 1024 * 1024
    ARTIFACT_OWNER_MAX_FILES: int = 1000
    ARTIFACT_REQUIRE_MALWARE_SCAN: bool = False
    ARTIFACT_STORAGE_BACKEND: str = "local"
    ARTIFACT_S3_BUCKET: str = ""
    ARTIFACT_S3_PREFIX: str = "y-point/artifacts/"
    ARTIFACT_S3_ENDPOINT_URL: str = ""
    ARTIFACT_S3_REGION: str = ""
    ARTIFACT_S3_ACCESS_KEY_ID: str = ""
    ARTIFACT_S3_SECRET_ACCESS_KEY: str = ""

    REDIS_HOST: str = "127.0.0.1"
    REDIS_ENABLED: bool = False
    REDIS_PORT: int = 6379
    REDIS_PWD: str = ""
    REDIS_DB: int = 0
    DECODE_RESPONSES: bool = True
    CELERY_BROKER_URL: Optional[str] = None
    CELERY_RESULT_BACKEND: Optional[str] = None
    CELERY_TASK_QUEUE: str = "y_point_task_queue"

    SENSITIVE_HEADERS: str = "authorization,cookie,set-cookie,x-api-key"

    @property
    def ENV_NAME(self) -> str:
        return normalize_env(self.ENV)

    @property
    def IS_DEV(self) -> bool:
        return self.ENV_NAME == "development"

    @property
    def IS_TEST(self) -> bool:
        return self.ENV_NAME == "test"

    @property
    def IS_PROD(self) -> bool:
        return self.ENV_NAME == "production"

    @property
    def IS_STAGING(self) -> bool:
        return self.ENV_NAME == "staging"

    @property
    def MASK_SENSITIVE_HEADERS(self) -> bool:
        return self.IS_PROD or self.IS_STAGING

    @property
    def redis_url(self) -> str:
        auth = f":{self.REDIS_PWD}@" if self.REDIS_PWD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}?decode_responses={self.DECODE_RESPONSES}"

    @property
    def redis_transport_url(self) -> str:
        auth = f":{self.REDIS_PWD}@" if self.REDIS_PWD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def celery_broker_url(self) -> str:
        return self.CELERY_BROKER_URL or self.redis_transport_url

    @property
    def celery_result_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.redis_transport_url

    @property
    def mysql_url(self) -> str:
        """
        Mysql连接(同步)
        :return:
        """
        return "mysql://{}:{}@{}:{}/{}".format(
            self.MYSQL_USERNAME,
            quote_plus(self.MYSQL_PASSWORD),
            self.MYSQL_HOSTNAME,
            self.MYSQL_PORT,
            self.MYSQL_DATABASE
        )

    @property
    def mysql_async_url(self) -> str:
        """
        Mysql连接(异步)
        :return:
        """
        return "mysql+asyncmy://{}:{}@{}:{}/{}".format(
            self.MYSQL_USERNAME,
            quote_plus(self.MYSQL_PASSWORD),
            self.MYSQL_HOSTNAME,
            self.MYSQL_PORT,
            self.MYSQL_DATABASE
        )

    @property
    def pg_url(self) -> str:
        """
        PostgreSQL(同步)
        :return:
        """
        return "postgres://{}:{}@{}:{}/{}".format(
            self.POSTGRESQL_USERNAME,
            quote_plus(self.POSTGRESQL_PASSWORD),
            self.POSTGRESQL_HOSTNAME,
            self.POSTGRESQL_PORT,
            self.POSTGRESQL_DATABASE
        )

    @property
    def pg_async_url(self) -> str:
        """
        PostgreSQL(异步)
        :return:
        """
        return "postgresql+asyncpg://{}:{}@{}:{}/{}".format(
            self.POSTGRESQL_USERNAME,
            quote_plus(self.POSTGRESQL_PASSWORD),
            self.POSTGRESQL_HOSTNAME,
            self.POSTGRESQL_PORT,
            self.POSTGRESQL_DATABASE
        )

    @property
    def sqlite_async_url(self) -> str:
        """
        sqlite(异步)
        :return:
        """
        if self.SQLITE_DATABASE == ":memory:":
            return "sqlite+aiosqlite:///:memory:"
        path = Path(self.SQLITE_DATABASE).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        return f"sqlite+aiosqlite:///{path}"

    @property
    def database_url(self) -> str:
        """数据库连接"""

        backend = (self.DB_BACKEND or "mysql").lower()
        if backend == "mysql":
            return self.mysql_async_url
        if backend in {"postgres", "postgresql"}:
            return self.pg_async_url
        if backend == "sqlite":
            return self.sqlite_async_url
        raise ValueError(f"Unsupported DB_BACKEND: {self.DB_BACKEND}")


@lru_cache
def get_config(env: Optional[str] = None) -> BaseConfig:
    fast_api_env = normalize_env(env or os.getenv("FAST_API_ENV", DEFAULT_ENV))
    env_files = resolve_env_files(fast_api_env)
    logger.info(f"加载配置: env={fast_api_env}, files={[str(p) for p in env_files]}")
    conf = BaseConfig(_env_file=[str(p) for p in env_files])
    if conf.ENV_NAME != fast_api_env:
        logger.warning(f"ENV 不一致：FAST_API_ENV={fast_api_env}, 配置 ENV={conf.ENV_NAME}")
    return conf


if __name__ == "__main__":
    dev_config = get_config("development")
    print(dev_config.model_dump())
    print(dev_config.APP_NAME)
    print(dev_config.MYSQL_HOSTNAME)

    pro_config = get_config("production")
    print(pro_config.model_dump())
