# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : __init__.py

# 新增模型后，请在这里导入，供 Alembic 自动发现
from app.models.admin import Admin
from app.models.aps_task import ApsTask
from app.models.auth_session import AuthSession
from app.models.auth_attempt import AuthAttempt
from app.models.audit_event import AuditEvent
from app.models.provider_connection import ProviderConnection
from app.models.model_catalog import ModelCatalog
from app.models.model_call_record import ModelCallRecord
from app.models.agent_run import AgentRun
from app.models.agent_config import AgentConfig
from app.models.agent_session import AgentSession
from app.models.agent_run_event import AgentRunEvent
from app.models.agent_run_message import AgentRunMessage
from app.models.agent_message_attachment import AgentMessageAttachment
from app.models.run_approval import RunApproval
from app.models.run_worker_heartbeat import RunWorkerHeartbeat
from app.models.artifact import Artifact

__all__ = [
    "Admin",
    "ApsTask",
    "AuthSession",
    "AuthAttempt",
    "AuditEvent",
    "ProviderConnection",
    "ModelCatalog",
    "ModelCallRecord",
    "AgentRun",
    "AgentConfig",
    "AgentSession",
    "AgentRunEvent",
    "AgentRunMessage",
    "AgentMessageAttachment",
    "RunWorkerHeartbeat",
    "Artifact",
]
