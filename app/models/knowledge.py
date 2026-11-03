# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:45:44
# @Author  : yangyuexiong
# @File    : knowledge.py

"""知识库、不可变文档与索引代、处理任务及运行检索引用。"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Integer, JSON, Numeric, String, Text, UniqueConstraint, event
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import KnowledgeJobState, KnowledgeIndexState, KnowledgeSearchMode
from app.models.base import CustomBaseModel, ShanghaiDateTime


class KnowledgeBase(CustomBaseModel):
    """知识库由创建者管理，活动索引仅在完整构建后原子切换。"""

    __table_name__ = "knowledge_base"
    __table_args__ = (
        {
            "comment": "私有知识库与索引配置",
        },
    )

    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="知识库所有者 ID")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="知识库名称")
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True, comment="知识库说明")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="最新配置及文档清单版本")
    active_index_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="完整发布的活动索引代 ID")
    search_mode: Mapped[str] = mapped_column(String(16), nullable=False, default=KnowledgeSearchMode.KEYWORD, comment="关键词或混合检索")
    embedding_model_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="实际验证的嵌入模型 ID")
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False, default=800, comment="规范提取文本的分块字符上限")
    chunk_overlap: Mapped[int] = mapped_column(Integer, nullable=False, default=100, comment="相邻片段重叠字符数")
    embedding_token_budget: Mapped[int] = mapped_column(Integer, nullable=False, default=200000, comment="单次构建的嵌入输入预算")
    embedding_cost_budget: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True, comment="单次构建的嵌入估算费用上限")


class KnowledgeShare(CustomBaseModel):
    """指定用户只读授权，与 Agent 共享分别管理。"""

    __table_name__ = "knowledge_share"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "user_id", name="uq_yp_knowledge_share"),
        {
            "comment": "知识库指定只读共享",
        },
    )

    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="知识库 ID")
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="获得只读授权的用户 ID")


class KnowledgeDocument(CustomBaseModel):
    """文档标识保持稳定，更新生成新上传版本，删除立即阻断旧代检索。"""

    __table_name__ = "knowledge_document"
    __table_args__ = (
        {
            "comment": "知识库文档与当前上传版本",
        },
    )

    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="知识库 ID")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, comment="展示用文件名")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, comment="当前上传版本号")


class KnowledgeDocumentVersion(CustomBaseModel):
    """保存私有文件引用及规范提取原文，引用位置对应此不可变版本。"""

    __table_name__ = "knowledge_document_version"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_yp_knowledge_document_version"),
        {
            "comment": "知识文档上传与提取版本",
        },
    )

    document_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="文档 ID")
    version: Mapped[int] = mapped_column(Integer, nullable=False, comment="上传版本号")
    artifact_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="私有原始文件 ID")
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, comment="原始文件摘要")
    filename: Mapped[str] = mapped_column(String(255), nullable=False, comment="该上传版本的原始文件名")
    media_type: Mapped[str] = mapped_column(String(128), nullable=False, comment="校验后的原始文件类型")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, comment="原始文件字节数")
    extracted_text: Mapped[str | None] = mapped_column(
        Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True, comment="规范提取原文，片段位置以此为准"
    )
    blocks: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="提取块的页码标题及原文偏移")


class KnowledgeIndex(CustomBaseModel):
    """一代完整索引绑定文档清单及嵌入身份，发布失败保留旧代。"""

    __table_name__ = "knowledge_index"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "revision", name="uq_yp_knowledge_index_revision"),
        {
            "comment": "知识库不可变索引代",
        },
    )

    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="知识库 ID")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, comment="调度时知识库配置版本")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default=KnowledgeIndexState.BUILDING, comment="索引构建及发布状态")
    configuration: Mapped[dict] = mapped_column(JSON, nullable=False, comment="固定的检索分块及模型维度配置")
    document_versions: Mapped[list] = mapped_column(JSON, nullable=False, comment="此代固定的文档版本 ID 清单")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已发布片段数量")


class KnowledgeChunk(CustomBaseModel):
    """片段与原文位置及向量一起发布，关键词采用数据库原生全文检索。"""

    __table_name__ = "knowledge_chunk"
    __table_args__ = (
        UniqueConstraint("index_id", "document_version_id", "position", name="uq_yp_knowledge_chunk_position"),
        {
            "comment": "知识索引片段与向量",
        },
    )

    index_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="固定索引代 ID")
    document_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="文档 ID")
    document_version_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="原文上传版本 ID")
    position: Mapped[int] = mapped_column(Integer, nullable=False, comment="版本内片段序号")
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="与规范提取原文偏移完全一致的片段")
    search_terms: Mapped[str] = mapped_column(Text, nullable=False, comment="跨数据库一致的中文二元及拉丁词项")
    start_offset: Mapped[int] = mapped_column(Integer, nullable=False, comment="规范提取原文起始字符位置")
    end_offset: Mapped[int] = mapped_column(Integer, nullable=False, comment="规范提取原文结束字符位置")
    page: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="PDF 实际页号，其他格式为空")
    heading: Mapped[str | None] = mapped_column(String(512), nullable=True, comment="所属章节标题")
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True, comment="固定模型和维度的单位长度向量")


class KnowledgeJob(CustomBaseModel):
    """任务租约及阶段持久化，取消、超时和重试都不能发布过期代。"""

    __table_name__ = "knowledge_job"
    __table_args__ = (
        {
            "comment": "知识索引异步处理任务",
        },
    )

    index_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True, comment="目标索引代 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="任务所属用户 ID")
    state: Mapped[str] = mapped_column(String(24), nullable=False, default=KnowledgeJobState.UPLOADED, index=True, comment="处理阶段及终态")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="已领取尝试次数")
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True, comment="当前领取租约令牌")
    lease_expires_at: Mapped[datetime | None] = mapped_column(ShanghaiDateTime(), nullable=True, comment="租约到期时间")
    failure_code: Mapped[str | None] = mapped_column(String(48), nullable=True, comment="脱敏失败类别")
    progress: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="阶段履历及明确跳过原因")
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="跨重试保守预留的嵌入输入用量")
    embedding_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="上游已报告嵌入输入用量")
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True, comment="已报告输入用量对应的估算费用")


class AgentKnowledgeBase(CustomBaseModel):
    """Agent 保存可选知识库，实际用户的授权在每轮重新核对。"""

    __table_name__ = "agent_knowledge_base"
    __table_args__ = (
        UniqueConstraint("agent_id", "knowledge_base_id", name="uq_yp_agent_knowledge_base"),
        {
            "comment": "智能体关联知识库",
        },
    )

    agent_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="智能体 ID")
    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="关联知识库 ID")


class RunKnowledgeBase(CustomBaseModel):
    """运行固定实际可访问的索引代，后续发布不改变此轮检索。"""

    __table_name__ = "run_knowledge_base"
    __table_args__ = (
        UniqueConstraint("run_id", "knowledge_base_id", name="uq_yp_run_knowledge_base"),
        {
            "comment": "运行知识库索引快照",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    knowledge_base_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="知识库 ID")
    index_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="提交时发布的索引代，未就绪时为空")


class KnowledgeRetrieval(CustomBaseModel):
    """保存检索审计与固定引用，事件中不保存文档或查询正文。"""

    __table_name__ = "knowledge_retrieval"
    __table_args__ = (
        {
            "comment": "运行知识检索脱敏审计",
        },
    )

    run_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True, comment="运行 ID")
    owner_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="实际检索用户 ID")
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False, comment="查询正文 SHA256")
    references: Mapped[list] = mapped_column(JSON, nullable=False, comment="实际返回的索引及片段引用，不含正文")
    embedding_usage: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="本次各模型嵌入用量及估算费用")
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="实际返回命中数")
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="实际交付片段字节数")
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, comment="检索耗时毫秒数")


@event.listens_for(KnowledgeChunk.__table__, "after_create")
def _create_knowledge_keyword_index(_target, connection, **_kwargs) -> None:
    """测试和部署建表时都创建同方言全文设施，保持 ORM 与迁移一致。"""
    from app.services.knowledge_backend import create_native_keyword_index

    create_native_keyword_index(connection)


@event.listens_for(KnowledgeChunk.__table__, "before_drop")
def _drop_knowledge_keyword_index(_target, connection, **_kwargs) -> None:
    """SQLite 删除片段表时先删除虚拟表及其内部辅助表。"""
    if connection.dialect.name == "sqlite":
        connection.exec_driver_sql("DROP TABLE IF EXISTS yp_knowledge_fts")
