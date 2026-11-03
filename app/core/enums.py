# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : enums.py

from enum import Enum, StrEnum


class UserStatus(str, Enum):
    normal = "正常"
    disable = "禁用"


class ToolKind(StrEnum):
    """工具执行来源。"""

    SCRIPT = "script"
    MCP = "mcp"


class ScriptLanguage(StrEnum):
    """沙箱支持的脚本语言。"""

    PYTHON = "python"
    BASH = "bash"


class Visibility(StrEnum):
    """用户资源的可见范围。"""

    PRIVATE = "private"
    PUBLIC = "public"


class ToolEffect(StrEnum):
    """工具的副作用类别。"""

    READ = "read"
    WRITE = "write"


class RecoveryPolicy(StrEnum):
    """工具执行后的恢复策略。"""

    SAFE = "safe"
    IDEMPOTENT = "idempotent"
    COMPENSATED = "compensated"
    MANUAL = "manual"


class ToolInvocationState(StrEnum):
    """用户工具调用的审计状态。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RunState(StrEnum):
    """运行任务的持久化状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ApprovalState(StrEnum):
    """人工审批请求的状态。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    """用户提交的审批决定。"""

    APPROVE = "approve"
    REJECT = "reject"


class ArtifactState(StrEnum):
    """附件和生成文件的生命周期。"""

    PENDING = "pending"
    READY = "ready"
    DELETED = "deleted"


class ArtifactOrigin(StrEnum):
    """文件进入平台的来源。"""

    UPLOAD = "upload"
    GENERATED = "generated"


class UserRole(StrEnum):
    """平台用户的访问角色。"""

    ADMIN = "admin"
    USER = "user"


class MessageRole(StrEnum):
    """平台持久化的会话消息角色。"""

    USER = "user"
    ASSISTANT = "assistant"


class ProviderKind(StrEnum):
    """受支持的模型供应商接入类型。"""

    OPENAI_COMPATIBLE = "openai_compatible"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    AZURE_OPENAI = "azure_openai"
    GEMINI = "gemini"
    BEDROCK = "bedrock"


class ModelCapability(StrEnum):
    """模型声明或实际验证的能力。"""

    TEXT = "text"
    VISION = "vision"
    TOOLS = "tools"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    REASONING_SUMMARY = "reasoning_summary"


class RunEventType(StrEnum):
    """平台公开运行事件类型。"""

    STATE = "state"
    TEXT_DELTA = "text_delta"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    ARTIFACT_PUBLISHED = "artifact_published"


class ToolEnvironmentState(StrEnum):
    """用户脚本依赖环境的准备状态。"""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class ToolEnvironmentFailure(StrEnum):
    """向所有者公开的脱敏环境准备失败类别。"""

    IMAGE_UNAVAILABLE = "image_unavailable"
    DEPENDENCIES_UNAVAILABLE = "dependencies_unavailable"
    INSTALL_FAILED = "install_failed"
    BUILD_TIMEOUT = "build_timeout"
    SIZE_LIMIT = "size_limit"
    INVALID_ARTIFACT = "invalid_artifact"
    WORKER_INTERRUPTED = "worker_interrupted"
    CONFIGURATION_ERROR = "configuration_error"


class ToolLeaseKind(StrEnum):
    """工具执行及依赖准备的并发资源类别。"""

    CALL = "call"
    PREPARATION = "preparation"
