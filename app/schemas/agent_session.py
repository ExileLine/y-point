# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 22:23:43
# @Author  : yangyuexiong
# @File    : agent_session.py

"""Agent 会话管理及会话内消息结构。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.schemas.artifact import ArtifactReferenceOut, GeneratedArtifactReferenceOut
from app.schemas.run import RunOut


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


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(description="会话 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    agent_id: int = Field(description="所属智能体 ID", examples=[1])
    thread_id: str = Field(description="专属检查点线程 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    title: str = Field(description="会话标题", examples=["产品需求讨论"])
    archived: bool = Field(description="是否已归档", examples=[False])
    version: int = Field(description="会话版本号", examples=[1])
    create_time: datetime = Field(description="创建时间", examples=["2026-10-26T22:00:00"])
    update_time: datetime = Field(description="更新时间", examples=["2026-10-26T22:00:00"])


class SessionPageOut(BaseModel):
    items: list[SessionOut] = Field(description="当前页会话", examples=[[]])
    total: int = Field(description="符合条件的会话数量", examples=[1])
    page: int = Field(description="页码", examples=[1])
    page_size: int = Field(description="每页数量", examples=[20])


class SessionRunCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_text: str = Field(min_length=1, max_length=32768, description="本轮用户输入", examples=["请总结需求"])
    request_key: str = Field(min_length=1, max_length=128, description="本轮提交幂等键", examples=["session-001"])
    publish_answer_artifact: bool = Field(
        default=False, description="是否将本轮最终回答发布为可下载的 TXT 产物", examples=[False],
    )
    artifact_ids: list[ArtifactId] = Field(
        default_factory=list, max_length=5, description="本轮引用的已上传私有文件 ID，按展示顺序排列",
    )
    context_artifact_ids: list[ArtifactId] = Field(
        default_factory=list, max_length=5, description="本轮需要读取内容的 TXT 附件 ID，必须属于 artifact_ids",
        examples=[["550e8400-e29b-41d4-a716-446655440000"]],
    )


class SessionRunPageOut(BaseModel):
    items: list[RunOut] = Field(description="当前页运行", examples=[[]])
    total: int = Field(description="该会话运行总数", examples=[1])
    page: int = Field(description="页码", examples=[1])
    page_size: int = Field(description="每页数量", examples=[20])


class SessionMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="消息游标 ID", examples=[1])
    run_id: str = Field(description="所属运行 ID", examples=["550e8400-e29b-41d4-a716-446655440000"])
    role: Literal["user", "assistant"] = Field(description="消息角色", examples=["assistant"])
    content: str = Field(description="消息正文", examples=["已整理需求"])
    create_time: datetime = Field(description="消息创建时间", examples=["2026-10-26T22:00:00"])
    attachments: list[ArtifactReferenceOut] = Field(default_factory=list, description="用户消息引用的上传文件")
    artifacts: list[GeneratedArtifactReferenceOut] = Field(default_factory=list, description="助手消息关联的生成产物")
