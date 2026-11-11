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


class ToolFeedbackFailure(StrEnum):
    """工具可反馈给模型修正的输入问题，执行与审计状态分别判断。"""

    INVALID_ARGUMENTS = "invalid_tool_arguments"
    SCRIPT_INPUT_REJECTED = "script_input_rejected"
    GENERATED_ARTIFACT_UNAVAILABLE = "generated_artifact_unavailable"


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


class ArtifactCleanupResult(StrEnum):
    """开发文件清理的逐项物理删除结果。"""

    DELETED = "deleted"
    ALREADY_MISSING = "already_missing"
    FAILED = "failed"


class ArtifactPreviewKind(StrEnum):
    """私有文件内容预览的展示方式。"""

    TEXT = "text"
    MARKDOWN = "markdown"
    HTML = "html"
    SHEETS = "sheets"


class ArtifactOrigin(StrEnum):
    """文件进入平台的来源。"""

    UPLOAD = "upload"
    SKILL_RESOURCE = "skill_resource"
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


class ModelThinkingMode(StrEnum):
    """单次模型调用的思考偏好，默认遵循上游设置。"""

    AUTO = "auto"
    ENABLED = "enabled"
    DISABLED = "disabled"


class RunTimeoutReason(StrEnum):
    """运行超时的机器可读原因，区分上游请求与执行预算。"""

    MODEL = "model_execution_timeout"
    EXECUTION = "execution_timeout"
    PROVIDER = "provider_timeout"
    TOOL = "tool_execution_timeout"


class RunRuntimeFailureReason(StrEnum):
    """运行编排失败的机器可读原因。"""

    GRAPH_STEP_LIMIT = "graph_step_limit_exceeded"
    TOOL_CAPACITY = "tool_capacity_exceeded"


class RunModelFailureReason(StrEnum):
    """模型生成失败的机器可读原因。"""

    UPSTREAM = "model_error"
    CONTEXT = "context_window_exceeded"
    EMPTY = "empty_response"
    SUMMARY = "summary_error"
    OUTPUT_LIMIT = "model_output_limit_exceeded"


class ModelOutputLimitReason(StrEnum):
    """统一大小写后的各供应商输出额度耗尽标记。"""

    LENGTH = "length"
    MAX_TOKENS = "max_tokens"
    BEDROCK_MAX_TOKENS = "maxtokens"
    MAX_OUTPUT_TOKENS = "max_output_tokens"


class SessionModelReason(StrEnum):
    """会话模型选择失败的机器可读原因。"""

    UNAVAILABLE = "model_unavailable"
    CAPABILITY = "model_capability_mismatch"
    CONTEXT = "model_context_insufficient"
    PRICING = "model_pricing_missing"
    BUSY = "session_busy"
    ARCHIVED = "session_archived"
    VERSION = "session_version_changed"
    SELECTION = "session_model_changed"
    THINKING = "model_thinking_mode_unsupported"
    OUTPUT = "model_output_limit_required"


class ModelCapability(StrEnum):
    """模型声明或实际验证的能力。"""

    TEXT = "text"
    EMBEDDING = "embedding"
    VISION = "vision"
    TOOLS = "tools"
    STREAMING = "streaming"
    STRUCTURED_OUTPUT = "structured_output"
    REASONING_SUMMARY = "reasoning_summary"


class RunCallKind(StrEnum):
    """公开调用摘要的资源类型。"""

    SKILL = "skill"
    TOOL = "tool"
    KNOWLEDGE = "knowledge"


class RunCallStatus(StrEnum):
    """调用事件确认的执行状态，终态缺失时不推断执行结果。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


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


class KnowledgeSearchMode(StrEnum):
    """知识检索组合策略。"""

    KEYWORD = "keyword"
    HYBRID = "hybrid"


class KnowledgeIndexState(StrEnum):
    """不可变索引代的发布状态。"""

    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeJobState(StrEnum):
    """文档异步处理阶段及可恢复终态。"""

    UPLOADED = "uploaded"
    PARSING = "parsing"
    CHUNKING = "chunking"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"


class KnowledgeFailure(StrEnum):
    """知识任务可对外公开的脱敏失败类别。"""

    PARSE_FAILED = "parse_failed"
    OCR_REQUIRED = "ocr_required"
    SIZE_LIMIT = "size_limit"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    WORKER_INTERRUPTED = "worker_interrupted"
    TIMEOUT = "timeout"
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    UPSTREAM = "upstream"
    INVALID_EMBEDDING = "invalid_embedding"
    MODEL_UNAVAILABLE = "model_unavailable"
    BUDGET_EXCEEDED = "budget_exceeded"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class KnowledgeVectorBackend(StrEnum):
    """知识向量距离计算适配。"""

    EXACT = "exact"
    PGVECTOR = "pgvector"


class KnowledgeRerank(StrEnum):
    """不依赖额外外部服务的可选重排策略。"""

    NONE = "none"
    COVERAGE = "coverage"


class SkillFileKind(StrEnum):
    """Skill 文件包中用于界面和能力校验的资源类别。"""

    INSTRUCTION = "instruction"
    REFERENCE = "reference"
    SCRIPT = "script"
    TEMPLATE = "template"
    SCHEMA = "schema"
    ASSET = "asset"
    TEXT = "text"
