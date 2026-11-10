# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 14:52:58
# @Author  : yangyuexiong
# @File    : tool.py

"""用户 Tool、不可变版本及 Agent/Run 调用关系。"""

from datetime import datetime

from sqlalchemy import Boolean, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import RecoveryPolicy, ToolEnvironmentState
from app.models.base import CustomBaseModel, ShanghaiDateTime


class UserTool(CustomBaseModel):
    __table_name__ = "user_tool"
    __table_args__ = (
        UniqueConstraint("owner_id", "name", name="uq_yp_user_tool_owner_name"),
        {
            "comment": "用户创建的脚本或 MCP Tool",
        },
    )

    skill_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True, comment="所属 Skill ID，空表示独立 Tool")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="创建用户 ID")
    name: Mapped[str] = mapped_column(String(64), nullable=False, comment="创建者范围内的 Tool 名称")
    description: Mapped[str] = mapped_column(String(1024), nullable=False, comment="模型可见的 Tool 用途说明")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, comment="脚本或 MCP 来源类型")
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, comment="私有或公开")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, comment="是否启用")
    credentials_revoked_through: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已撤销凭据的最大 Tool 版本号")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="当前发布版本号")


class UserToolVersion(CustomBaseModel):
    __table_name__ = "user_tool_version"
    __table_args__ = (
        UniqueConstraint("tool_id", "version", name="uq_yp_user_tool_version_number"),
        {
            "comment": "用户 Tool 不可变版本",
        },
    )

    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="Tool ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本号")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="执行定义 SHA256")
    input_schema: Mapped[dict] = mapped_column(JSON, nullable=False, comment="工具输入 JSON Schema")
    effect: Mapped[str] = mapped_column(String(16), nullable=False, comment="只读或外部写入")
    recovery_policy: Mapped[str] = mapped_column(String(24), nullable=False, default=RecoveryPolicy.MANUAL, comment="调用恢复策略")
    idempotency_key_field: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="远端幂等键参数名")
    write_contract_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="外部写恢复契约是否经管理员验收")
    write_contract_evidence_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="外部写恢复验收证据引用摘要")

    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, comment="调用前是否要求审批")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="当前版本是否通过发布验证")
    requirements: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="用户声明的 Python 依赖清单")
    environment_state: Mapped[str] = mapped_column(String(24), nullable=False, default=ToolEnvironmentState.NOT_REQUIRED, comment="脚本依赖环境准备状态")
    environment_spec_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="依赖声明与基础镜像的准备请求摘要")
    environment_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="依赖与基础镜像的环境缓存键")
    environment_image: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="已固定的沙箱基础镜像 ID")
    environment_lock: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="完整依赖版本和 wheel SHA256 清单")
    environment_manifest: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="已安装环境文件的路径与 SHA256 清单")
    environment_error_code: Mapped[str | None] = mapped_column(String(48), nullable=True, comment="环境准备脱敏失败类别")
    environment_error_detail: Mapped[str | None] = mapped_column(String(256), nullable=True, comment="可公开的缺失依赖说明")
    environment_lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="环境准备任务租约令牌")
    environment_lease_expires_at: Mapped[datetime | None] = mapped_column(ShanghaiDateTime(), nullable=True, comment="环境准备租约到期时间")
    environment_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="环境准备尝试次数")
    script_language: Mapped[str | None] = mapped_column(String(16), nullable=True, comment="脚本语言")
    source_code: Mapped[str | None] = mapped_column(Text, nullable=True, comment="脚本源代码")
    mcp_url: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="MCP Streamable HTTP 地址")
    mcp_tool_name: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="远端 MCP 工具名")
    encrypted_credentials: Mapped[str | None] = mapped_column(Text, nullable=True, comment="加密的 MCP 连接凭据")
    share_credentials: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, comment="公开时是否授权共享连接凭据")


class AgentTool(CustomBaseModel):
    __table_name__ = "agent_tool"
    __table_args__ = (
        UniqueConstraint("agent_id", "tool_id", name="uq_yp_agent_tool_pair"),
        {
            "comment": "智能体直接关联的 Tool 版本",
        },
    )

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="智能体 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="固定 Tool 版本号")


class RunTool(CustomBaseModel):
    __table_name__ = "run_tool"
    __table_args__ = (
        UniqueConstraint("run_id", "tool_id", name="uq_yp_run_tool_pair"),
        {
            "comment": "运行可调用的 Tool 版本快照",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="运行固定版本号")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="运行固定执行定义摘要")


class ToolInvocation(CustomBaseModel):
    __table_name__ = "tool_invocation"
    __table_args__ = (
        UniqueConstraint("run_id", "call_id", name="uq_yp_tool_invocation_call"),
        {
            "comment": "用户 Tool 调用审计记录",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    call_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="模型工具调用 ID")
    tool_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="Tool ID")
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False, comment="调用 Tool 版本号")
    state: Mapped[str] = mapped_column(String(24), nullable=False, comment="调用状态")
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="输入参数 SHA256")
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True, comment="脱敏失败类别")
