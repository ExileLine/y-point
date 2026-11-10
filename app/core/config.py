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
from pydantic import Field, model_validator
from urllib.parse import quote_plus
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import KnowledgeVectorBackend


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
    """将环境名称或别名规范化为受支持的配置环境。"""
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

    raise FileNotFoundError(f"未找到配置文件，请创建 {ENV_FILE_MAP.get(env_name)} 或设置 {ENV_FILE_ENV_VAR}")


BASE_SETTINGS_CONFIG = SettingsConfigDict(env_file_encoding="utf-8", case_sensitive=False, extra="ignore")


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

    SQLITE_DATABASE: str = "y_point.db"
    CHECKPOINT_BACKEND: str = "sqlite"
    CHECKPOINT_POSTGRES_DSN: str = ""
    RUNTIME_RECURSION_LIMIT: int = Field(default=9999, ge=1, description="每次运行或恢复的 LangGraph 最大图步数，非时间限制", examples=[9999])

    # 产物配置参数
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
    ARTIFACT_UPLOAD_MAX_BYTES: int = Field(default=20 * 1024 * 1024, gt=0, description="单个对话上传文件字节上限", examples=[20971520])
    ATTACHMENT_CONTEXT_MAX_BYTES: int = Field(default=20 * 1024 * 1024, gt=0, description="本轮附件提取文本及元数据的 UTF-8 字节上限", examples=[20971520])

    # 开发日志配置参数
    WORKER_LOG_FILE: str = ".logs/worker.log"
    WORKER_LOG_MAX_BYTES: int = Field(default=50 * 1024 * 1024, gt=0, description="worker 日志文件容量上限，单位字节", examples=[52428800])

    # AGENT配置参数
    AGENT_DEFAULT_MODEL_TIMEOUT_SECONDS: int = Field(default=120, gt=0, description="新建 Agent 或独立 Run 的默认单次模型超时秒数", examples=[300])
    AGENT_MAX_MODEL_TIMEOUT_SECONDS: int = Field(default=3600, gt=0, description="允许设置的单次模型或供应商请求超时秒数上限", examples=[3600])
    AGENT_DEFAULT_TOOL_TIMEOUT_SECONDS: int = Field(default=120, gt=0, description="新建 Agent 或独立 Run 的默认单次工具超时秒数", examples=[120])
    AGENT_MAX_TOOL_TIMEOUT_SECONDS: int = Field(default=3600, gt=0, description="允许设置的单次工具超时秒数上限，具体工具可有更严限制", examples=[3600])
    AGENT_DEFAULT_MAX_OUTPUT_TOKENS: int = Field(default=4096, gt=0, description="新建 Agent 或独立 Run 默认单次模型输出 Token 上限", examples=[8192])
    AGENT_MAX_OUTPUT_TOKENS: int = Field(default=65536, gt=0, description="允许设置的单次输出 Token 上限", examples=[65536])

    # SCRIPT配置参数
    SCRIPT_SANDBOX_IMAGE: str = "y-point-script-sandbox:1"
    SCRIPT_DEPENDENCY_ALLOW_NETWORK: bool = True
    SCRIPT_DEPENDENCY_INDEX_URL: str = "https://pypi.org/simple"
    SCRIPT_DEPENDENCY_ALLOWED_HOSTS: list[str] = ["pypi.org", "files.pythonhosted.org"]
    SCRIPT_DEPENDENCY_WHEELHOUSE: str = ""
    SCRIPT_DEPENDENCY_STORAGE_DIR: str = ".run-private/tool-environments"
    SCRIPT_DEPENDENCY_BUILD_TIMEOUT_SECONDS: int = 180
    SCRIPT_EXECUTION_TIMEOUT_SECONDS: int = Field(default=30, gt=0, description="单次正式脚本执行时限", examples=[120])
    SCRIPT_TEST_TIMEOUT_SECONDS: int = Field(default=10, gt=0, description="Tool 及 Skill 脚本试运行时限", examples=[30])

    # MCP配置参数
    MCP_CONNECT_TIMEOUT_SECONDS: int = Field(default=10, gt=0, description="MCP HTTP 建连、写入及连接池等待超时秒数", examples=[10])
    MCP_DISCOVERY_TIMEOUT_SECONDS: int = Field(default=15, gt=0, description="单次 MCP 发现总时限，含握手及分页", examples=[15])
    MCP_CALL_TIMEOUT_SECONDS: int = Field(default=120, gt=0, description="单次正式 MCP 调用时限，含连接、握手及结构复核", examples=[300])
    MCP_DISCOVERY_MAX_TOOLS: int = Field(default=128, ge=1, le=1024, description="MCP 发现工具数量上限", examples=[128])
    MCP_DISCOVERY_MAX_BYTES: int = Field(default=256 * 1024, ge=1024, le=1024 * 1024, description="MCP 发现结果总字节上限", examples=[262144])
    MCP_RESPONSE_MAX_BYTES: int = Field(default=1024 * 1024, ge=1024, le=16 * 1024 * 1024, description="MCP 单次连接接收字节上限", examples=[1048576])

    # 知识库配置参数
    KNOWLEDGE_VECTOR_BACKEND: KnowledgeVectorBackend = Field(
        default=KnowledgeVectorBackend.EXACT,
        description="知识向量距离后端，pgvector 须在 PostgreSQL 显式安装",
        examples=["exact"],
    )
    KNOWLEDGE_SEARCH_TIMEOUT_SECONDS: int = Field(default=60, gt=0, description="单次知识检索时限，HTTP 与 Agent 共用", examples=[60])
    KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS: int = Field(default=60, gt=0, description="单批嵌入请求时限，检索及索引共用", examples=[60])

    # TOOL配置参数
    TOOL_CALL_GLOBAL_LIMIT: int = Field(default=16, ge=1, le=1024, description="全部进程的工具调用并发上限", examples=[16])
    TOOL_CALL_USER_LIMIT: int = Field(default=4, ge=1, le=1024, description="单用户工具调用并发上限", examples=[4])
    TOOL_CALL_RUN_LIMIT: int = Field(default=2, ge=1, le=128, description="单 Run 工具调用并发上限", examples=[2])
    TOOL_PREPARATION_GLOBAL_LIMIT: int = Field(default=2, ge=1, le=128, description="全部进程的依赖准备并发上限", examples=[2])
    TOOL_PREPARATION_USER_LIMIT: int = Field(default=1, ge=1, le=128, description="单用户依赖准备并发上限", examples=[1])
    TOOL_STORAGE_MAX_BYTES: int = Field(default=8 * 1024**3, ge=1024, description="依赖缓存和准备目录的应用容量预算", examples=[8589934592])
    TOOL_STORAGE_MIN_FREE_BYTES: int = Field(default=1024**3, ge=0, description="准备前必须保留的文件系统可用字节", examples=[1073741824])
    TOOL_RESOURCE_RECONCILE_SECONDS: int = Field(default=60, ge=1, description="工具资源核对间隔秒数", examples=[60])
    TOOL_RESOURCE_GRACE_SECONDS: int = Field(default=300, ge=30, description="过期资源清理宽限秒数", examples=[300])

    @model_validator(mode="after")
    def validate_runtime_defaults(self) -> "BaseConfig":
        """启动时拒绝默认值大于对应可配置上限的配置。"""
        for name in ("MODEL_TIMEOUT_SECONDS", "TOOL_TIMEOUT_SECONDS", "MAX_OUTPUT_TOKENS"):
            maximum = "AGENT_MAX_OUTPUT_TOKENS" if name == "MAX_OUTPUT_TOKENS" else f"AGENT_MAX_{name}"
            if getattr(self, f"AGENT_DEFAULT_{name}") > getattr(self, maximum):
                raise ValueError(f"AGENT_DEFAULT_{name} 不能超过 {maximum}")
        return self

    @property
    def ENV_NAME(self) -> str:
        """返回当前环境的规范名称。"""
        return normalize_env(self.ENV)

    @property
    def IS_DEV(self) -> bool:
        """判断当前是否为开发环境。"""
        return self.ENV_NAME == "development"

    @property
    def IS_TEST(self) -> bool:
        """判断当前是否为测试环境。"""
        return self.ENV_NAME == "test"

    @property
    def IS_PROD(self) -> bool:
        """判断当前是否为生产环境。"""
        return self.ENV_NAME == "production"

    @property
    def IS_STAGING(self) -> bool:
        """判断当前是否为预发布环境。"""
        return self.ENV_NAME == "staging"

    @property
    def MASK_SENSITIVE_HEADERS(self) -> bool:
        """判断当前环境是否需要屏蔽敏感请求头。"""
        return self.IS_PROD or self.IS_STAGING

    @property
    def redis_url(self) -> str:
        """构建包含解码选项的 Redis 连接地址。"""
        auth = f":{self.REDIS_PWD}@" if self.REDIS_PWD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}?decode_responses={self.DECODE_RESPONSES}"

    @property
    def redis_transport_url(self) -> str:
        """构建供任务传输层使用的 Redis 连接地址。"""
        auth = f":{self.REDIS_PWD}@" if self.REDIS_PWD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def celery_broker_url(self) -> str:
        """返回显式配置或基于 Redis 的任务代理地址。"""
        return self.CELERY_BROKER_URL or self.redis_transport_url

    @property
    def celery_result_backend(self) -> str:
        """返回显式配置或基于 Redis 的任务结果存储地址。"""
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
            self.MYSQL_DATABASE,
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
            self.MYSQL_DATABASE,
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
            self.POSTGRESQL_DATABASE,
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
            self.POSTGRESQL_DATABASE,
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
    """按环境和本地覆盖文件加载并缓存应用配置。"""
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
