# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:23:43
# @Author  : yangyuexiong
# @File    : agent_session.py

"""Agent 会话管理及会话内消息结构。"""

from datetime import datetime
from typing import Annotated


from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.core.enums import MessageRole, ModelCapability, RunState, SessionModelReason
from app.schemas.artifact import ArtifactReferenceOut, GeneratedArtifactReferenceOut
from app.schemas.run import RunMetricsOut, RunCallOut, RunOut


SessionTitle = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
ArtifactId = Annotated[str, StringConstraints(min_length=36, max_length=36)]


class SessionCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: SessionTitle = Field(default="新对话", description="会话标题", examples=["产品需求讨论"])


class SessionUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, description="本次修改读取到的版本号", examples=[1])
    title: SessionTitle | None = Field(default=None, description="新会话标题", examples=["需求评审"])
    archived: bool | None = Field(default=None, description="是否归档；可取消归档", examples=[True])
    model_id: int | None = Field(default=None, gt=0, description="切换会话后续轮次使用的模型；不修改 Agent 默认模型", examples=[2])


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="会话 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    agent_id: int = Field(description="所属智能体 ID", examples=[1])
    thread_id: str = Field(description="专属检查点线程 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    model_id: int | None = Field(description="会话当前模型 ID；新会话继承 Agent 默认模型", examples=[1])
    title: str = Field(description="会话标题", examples=["产品需求讨论"])
    archived: bool = Field(description="是否已归档", examples=[False])
    version: int = Field(description="会话版本号", examples=[1])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-26T22:00:00"])
    update_time: datetime = Field(description="更新时间", examples=["2026-10-26T22:00:00"])


class SessionModelOptionOut(BaseModel):
    id: int = Field(description="模型 ID", examples=[1])
    display_name: str = Field(description="模型名称", examples=["本地对话模型"])
    provider_name: str = Field(description="供应商名称", examples=["本地 Ollama"])
    verified_capabilities: list[ModelCapability] = Field(description="已验证的模型能力", examples=[["text", "tools"]])
    context_window: int | None = Field(description="模型上下文窗口", examples=[32768])
    selectable: bool = Field(description="是否满足当前 Agent 的能力、窗口与预算要求", examples=[True])
    reason: SessionModelReason | None = Field(description="不可选原因码", examples=[None])
    notice: str | None = Field(description="不可选原因说明", examples=[None])


class SessionPageOut(BaseModel):
    items: list[SessionOut] = Field(description="当前页会话", examples=[[]])
    total: int = Field(description="符合条件的会话数量", examples=[1])
    page: int = Field(description="页码", examples=[1])
    page_size: int = Field(description="每页数量", examples=[20])


class SessionRunCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: int | None = Field(default=None, gt=0, description="预期会话模型；只校验选择，切换请使用修改会话接口", examples=[1])
    expected_session_version: int | None = Field(default=None, ge=1, description="提交时读取的会话版本；幂等重试优先返回原运行", examples=[1])
    input_text: str = Field(min_length=1, max_length=32768, description="本轮用户输入", examples=["请总结需求"])
    request_key: str = Field(min_length=1, max_length=128, description="本轮提交幂等键", examples=["session-001"])
    publish_answer_artifact: bool = Field(default=False, description="是否将本轮最终回答发布为可下载的 TXT 产物", examples=[False])
    artifact_ids: list[ArtifactId] = Field(
        default_factory=list,
        max_length=5,
        description="本轮引用的已上传私有文件 ID，按展示顺序排列",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )
    context_artifact_ids: list[ArtifactId] = Field(
        default_factory=list,
        max_length=5,
        description="本轮需要模型读取的文本或图片附件 ID，必须属于 artifact_ids",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )


class SessionMetricsOut(BaseModel):
    """整个会话所有轮次的累计时长和已报告用量。"""

    run_count: int = Field(ge=0, description="会话全部轮次数，包含失败与取消", examples=[24])
    active_run_count: int = Field(ge=0, description="尚未结束的轮次数，用于界面持续计时", examples=[1])
    elapsed_ms: int = Field(ge=0, description="各轮提交至首次终态或统计时刻的耗时之和，排除轮次间空闲", examples=[123400])
    total_tokens: int | None = Field(ge=0, description="每轮累计已报告 Token 之和；非空会话完全未报告时为空", examples=[18234])
    missing_duration_runs: int = Field(ge=0, description="缺少终态事件而无法计算耗时的历史轮次数", examples=[0])
    calculated_at: datetime = Field(description="统计时间，采用带时区的服务器时间", examples=["2026-11-11T22:00:00+08:00"])


class SessionRunPageOut(BaseModel):
    items: list[RunOut] = Field(description="当前页运行", examples=[[]])
    total: int = Field(description="该会话运行总数", examples=[1])
    page: int = Field(description="页码", examples=[1])
    page_size: int = Field(description="每页数量", examples=[20])


class SessionMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="消息游标 ID", examples=[1])
    run_id: str = Field(description="所属运行 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    model_id: int | None = Field(default=None, description="该消息所属运行实际使用的模型 ID", examples=[1])
    model_name: str | None = Field(default=None, description="模型当前目录名称；目录不存在时仅保留 ID", examples=["本地模型"])
    provider_name: str | None = Field(default=None, description="模型供应商当前名称", examples=["本地 Ollama"])
    role: MessageRole = Field(description="消息角色", examples=["assistant"])
    content: str = Field(description="消息正文", examples=["已整理需求"])
    reasoning_content: str = Field(default="", description="本轮模型显式公开的思考文本，按事件顺序恢复；旧轮次可能为空", examples=["先核对输入资料。"])
    partial_content: str = Field(
        default="", description="失败、超时或取消前已公开的正文；仅展示，不作为完成回答或后续模型上下文", examples=["已检查第一节，尚未完成。"]
    )
    run_state: RunState | None = Field(default=None, description="消息所属轮次的当前状态，用于标识未完成内容", examples=["failed"])
    failure_code: str | None = Field(default=None, description="消息所属轮次的脱敏故障类别", examples=["model_output_limit_exceeded"])
    metrics: RunMetricsOut | None = Field(default=None, description="本轮耗时和累计已报告用量", examples=[None])
    calls: list[RunCallOut] = Field(default_factory=list, description="消息所属运行实际调用的脱敏名称和状态；失败轮次可由用户消息恢复调用记录", examples=[[]])
    create_time: datetime = Field(description="消息创建时间", examples=["2026-10-26T22:00:00"])
    attachments: list[ArtifactReferenceOut] = Field(default_factory=list, description="用户消息引用的上传文件", examples=[[]])
    artifacts: list[GeneratedArtifactReferenceOut] = Field(default_factory=list, description="助手消息关联的生成产物", examples=[[]])


class SessionAttachmentCheckIn(BaseModel):
    """提交前按当前会话模型校验本轮附件。"""

    model_config = ConfigDict(extra="forbid")
    artifact_ids: list[ArtifactId] = Field(max_length=5, description="已上传的消息附件 ID", examples=[[]])
    context_artifact_ids: list[ArtifactId] = Field(max_length=5, description="进入模型上下文的附件 ID", examples=[[]])
