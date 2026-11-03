# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:55:55
# @Author  : yangyuexiong
# @File    : 042_knowledge.py

"""新增知识库完整版本索引、持久化处理任务及运行检索引用。"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from app.models.base import ShanghaiDateTime

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建带中文注释且无外键的知识资源，并安装当前方言全文索引。"""
    op.add_column("yp_model_catalog", sa.Column("embedding_dimensions", sa.Integer(), nullable=True, comment="实际嵌入探测通过的向量维度"))
    op.create_table(
        "yp_agent_knowledge_base",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="智能体 ID"),
        sa.Column("knowledge_base_id", sa.Integer(), nullable=False, comment="关联知识库 ID"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("agent_id", "knowledge_base_id", name="uq_yp_agent_knowledge_base"),
        comment="智能体关联知识库",
    )
    op.create_index("ix_yp_agent_knowledge_base_agent_id", "yp_agent_knowledge_base", ["agent_id"], unique=False)
    op.create_table(
        "yp_knowledge_base",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="知识库所有者 ID"),
        sa.Column("name", sa.String(128), nullable=False, comment="知识库名称"),
        sa.Column("description", sa.String(1024), nullable=True, comment="知识库说明"),
        sa.Column("revision", sa.Integer(), nullable=False, comment="最新配置及文档清单版本"),
        sa.Column("active_index_id", sa.Integer(), nullable=True, comment="完整发布的活动索引代 ID"),
        sa.Column("search_mode", sa.String(16), nullable=False, comment="关键词或混合检索"),
        sa.Column("embedding_model_id", sa.Integer(), nullable=True, comment="实际验证的嵌入模型 ID"),
        sa.Column("chunk_size", sa.Integer(), nullable=False, comment="规范提取文本的分块字符上限"),
        sa.Column("chunk_overlap", sa.Integer(), nullable=False, comment="相邻片段重叠字符数"),
        sa.Column("embedding_token_budget", sa.Integer(), nullable=False, comment="单次构建的嵌入输入预算"),
        sa.Column("embedding_cost_budget", sa.Numeric(18, 6), nullable=True, comment="单次构建的嵌入估算费用上限"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        comment="私有知识库与索引配置",
    )
    op.create_index("ix_yp_knowledge_base_owner_id", "yp_knowledge_base", ["owner_id"], unique=False)
    op.create_table(
        "yp_knowledge_chunk",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("index_id", sa.Integer(), nullable=False, comment="固定索引代 ID"),
        sa.Column("document_id", sa.Integer(), nullable=False, comment="文档 ID"),
        sa.Column("document_version_id", sa.Integer(), nullable=False, comment="原文上传版本 ID"),
        sa.Column("position", sa.Integer(), nullable=False, comment="版本内片段序号"),
        sa.Column("content", sa.Text(), nullable=False, comment="与规范提取原文偏移完全一致的片段"),
        sa.Column("search_terms", sa.Text(), nullable=False, comment="跨数据库一致的中文二元及拉丁词项"),
        sa.Column("start_offset", sa.Integer(), nullable=False, comment="规范提取原文起始字符位置"),
        sa.Column("end_offset", sa.Integer(), nullable=False, comment="规范提取原文结束字符位置"),
        sa.Column("page", sa.Integer(), nullable=True, comment="PDF 实际页号，其他格式为空"),
        sa.Column("heading", sa.String(512), nullable=True, comment="所属章节标题"),
        sa.Column("embedding", sa.JSON(), nullable=True, comment="固定模型和维度的单位长度向量"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("index_id", "document_version_id", "position", name="uq_yp_knowledge_chunk_position"),
        comment="知识索引片段与向量",
    )
    op.create_index("ix_yp_knowledge_chunk_document_id", "yp_knowledge_chunk", ["document_id"], unique=False)
    op.create_index("ix_yp_knowledge_chunk_index_id", "yp_knowledge_chunk", ["index_id"], unique=False)
    op.create_table(
        "yp_knowledge_document",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("knowledge_base_id", sa.Integer(), nullable=False, comment="知识库 ID"),
        sa.Column("filename", sa.String(255), nullable=False, comment="展示用文件名"),
        sa.Column("current_version", sa.Integer(), nullable=False, comment="当前上传版本号"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        comment="知识库文档与当前上传版本",
    )
    op.create_index("ix_yp_knowledge_document_knowledge_base_id", "yp_knowledge_document", ["knowledge_base_id"], unique=False)
    op.create_table(
        "yp_knowledge_document_version",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("document_id", sa.Integer(), nullable=False, comment="文档 ID"),
        sa.Column("version", sa.Integer(), nullable=False, comment="上传版本号"),
        sa.Column("artifact_id", sa.String(36), nullable=False, comment="私有原始文件 ID"),
        sa.Column("sha256", sa.String(64), nullable=False, comment="原始文件摘要"),
        sa.Column("filename", sa.String(255), nullable=False, comment="该上传版本的原始文件名"),
        sa.Column("media_type", sa.String(128), nullable=False, comment="校验后的原始文件类型"),
        sa.Column("byte_size", sa.Integer(), nullable=False, comment="原始文件字节数"),
        sa.Column("extracted_text", sa.Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=True, comment="规范提取原文，片段位置以此为准"),
        sa.Column("blocks", sa.JSON(), nullable=True, comment="提取块的页码标题及原文偏移"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("document_id", "version", name="uq_yp_knowledge_document_version"),
        comment="知识文档上传与提取版本",
    )
    op.create_index("ix_yp_knowledge_document_version_document_id", "yp_knowledge_document_version", ["document_id"], unique=False)
    op.create_table(
        "yp_knowledge_index",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("knowledge_base_id", sa.Integer(), nullable=False, comment="知识库 ID"),
        sa.Column("revision", sa.Integer(), nullable=False, comment="调度时知识库配置版本"),
        sa.Column("state", sa.String(24), nullable=False, comment="索引构建及发布状态"),
        sa.Column("configuration", sa.JSON(), nullable=False, comment="固定的检索分块及模型维度配置"),
        sa.Column("document_versions", sa.JSON(), nullable=False, comment="此代固定的文档版本 ID 清单"),
        sa.Column("chunk_count", sa.Integer(), nullable=False, comment="已发布片段数量"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("knowledge_base_id", "revision", name="uq_yp_knowledge_index_revision"),
        comment="知识库不可变索引代",
    )
    op.create_index("ix_yp_knowledge_index_knowledge_base_id", "yp_knowledge_index", ["knowledge_base_id"], unique=False)
    op.create_table(
        "yp_knowledge_job",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("index_id", sa.Integer(), nullable=False, comment="目标索引代 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="任务所属用户 ID"),
        sa.Column("state", sa.String(24), nullable=False, comment="处理阶段及终态"),
        sa.Column("attempts", sa.Integer(), nullable=False, comment="已领取尝试次数"),
        sa.Column("lease_token", sa.String(36), nullable=True, comment="当前领取租约令牌"),
        sa.Column("lease_expires_at", ShanghaiDateTime(), nullable=True, comment="租约到期时间"),
        sa.Column("failure_code", sa.String(48), nullable=True, comment="脱敏失败类别"),
        sa.Column("progress", sa.JSON(), nullable=False, comment="阶段履历及明确跳过原因"),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False, comment="跨重试保守预留的嵌入输入用量"),
        sa.Column("embedding_tokens", sa.Integer(), nullable=False, comment="上游已报告嵌入输入用量"),
        sa.Column("estimated_cost", sa.Numeric(18, 6), nullable=True, comment="已报告输入用量对应的估算费用"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("index_id"),
        comment="知识索引异步处理任务",
    )
    op.create_index("ix_yp_knowledge_job_owner_id", "yp_knowledge_job", ["owner_id"], unique=False)
    op.create_index("ix_yp_knowledge_job_state", "yp_knowledge_job", ["state"], unique=False)
    op.create_table(
        "yp_knowledge_retrieval",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="运行 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="实际检索用户 ID"),
        sa.Column("query_hash", sa.String(64), nullable=False, comment="查询正文 SHA256"),
        sa.Column("references", sa.JSON(), nullable=False, comment="实际返回的索引及片段引用，不含正文"),
        sa.Column("embedding_usage", sa.JSON(), nullable=False, comment="本次各模型嵌入用量及估算费用"),
        sa.Column("hit_count", sa.Integer(), nullable=False, comment="实际返回命中数"),
        sa.Column("byte_count", sa.Integer(), nullable=False, comment="实际交付片段字节数"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, comment="检索耗时毫秒数"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        comment="运行知识检索脱敏审计",
    )
    op.create_index("ix_yp_knowledge_retrieval_run_id", "yp_knowledge_retrieval", ["run_id"], unique=False)
    op.create_table(
        "yp_knowledge_share",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("knowledge_base_id", sa.Integer(), nullable=False, comment="知识库 ID"),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="获得只读授权的用户 ID"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("knowledge_base_id", "user_id", name="uq_yp_knowledge_share"),
        comment="知识库指定只读共享",
    )
    op.create_index("ix_yp_knowledge_share_knowledge_base_id", "yp_knowledge_share", ["knowledge_base_id"], unique=False)
    op.create_index("ix_yp_knowledge_share_user_id", "yp_knowledge_share", ["user_id"], unique=False)
    op.create_table(
        "yp_run_knowledge_base",
        sa.Column("id", sa.Integer(), nullable=False, primary_key=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="运行 ID"),
        sa.Column("knowledge_base_id", sa.Integer(), nullable=False, comment="知识库 ID"),
        sa.Column("index_id", sa.Integer(), nullable=True, comment="提交时发布的索引代，未就绪时为空"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("run_id", "knowledge_base_id", name="uq_yp_run_knowledge_base"),
        comment="运行知识库索引快照",
    )
    op.create_index("ix_yp_run_knowledge_base_run_id", "yp_run_knowledge_base", ["run_id"], unique=False)
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute("CREATE VIRTUAL TABLE yp_knowledge_fts USING fts5(terms, index_id UNINDEXED, chunk_id UNINDEXED)")
    elif dialect == "postgresql":
        op.execute("CREATE INDEX ix_yp_knowledge_native_terms ON yp_knowledge_chunk USING gin (to_tsvector('simple', search_terms))")
    elif dialect == "mysql":
        op.execute("CREATE FULLTEXT INDEX ix_yp_knowledge_native_terms ON yp_knowledge_chunk (search_terms)")


def downgrade() -> None:
    """先移除当前方言全文设施，再移除知识资源，不改动既有文件及对话表。"""
    if op.get_bind().dialect.name == "sqlite":
        op.execute("DROP TABLE IF EXISTS yp_knowledge_fts")
    op.drop_table("yp_run_knowledge_base")
    op.drop_table("yp_knowledge_share")
    op.drop_table("yp_knowledge_retrieval")
    op.drop_table("yp_knowledge_job")
    op.drop_table("yp_knowledge_index")
    op.drop_table("yp_knowledge_document_version")
    op.drop_table("yp_knowledge_document")
    op.drop_table("yp_knowledge_chunk")
    op.drop_table("yp_knowledge_base")
    op.drop_table("yp_agent_knowledge_base")
    op.drop_column("yp_model_catalog", "embedding_dimensions")
