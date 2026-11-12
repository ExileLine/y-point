# -*- coding: utf-8 -*-
# @Time    : 2026-11-12 14:50:42
# @Author  : yangyuexiong
# @File    : 050_knowledge_processing.py

"""添加知识文档分类处理、表格来源和语义加工缓存。

Revision ID: 050
Revises: 049
"""

from alembic import op
import sqlalchemy as sa

from app.models.base import ShanghaiDateTime

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def _columns() -> dict:
    """集中声明新增列，升级和回退使用同一完整清单。"""
    return {
        "yp_knowledge_base": [
            sa.Column("processing_mode", sa.String(16), nullable=False, server_default="auto", comment="原文或增强加工策略"),
            sa.Column("processing_model_id", sa.Integer(), nullable=True, comment="独立配置的文档加工模型 ID"),
            sa.Column("processing_token_budget", sa.Integer(), nullable=False, server_default="100000", comment="单次构建跨重试的加工预留 Token 上限"),
            sa.Column("processing_cost_budget", sa.Numeric(18, 6), nullable=True, comment="单次构建加工估算费用上限，按模型币种"),
        ],
        "yp_knowledge_document": [
            sa.Column("processing_options", sa.JSON(), nullable=False, server_default=sa.text("'{}'"), comment="文档处理覆盖配置，索引任务固定快照"),
        ],
        "yp_knowledge_document_version": [
            sa.Column("parser_fingerprint", sa.String(64), nullable=True, comment="解析器及文档选择配置的缓存摘要"),
        ],
        "yp_knowledge_chunk": [
            sa.Column("source_locator", sa.JSON(), nullable=True, comment="原文或表格工作表及单元格范围"),
            sa.Column("knowledge_notes", sa.Text(), nullable=True, comment="模型对本片段的解释，仅辅助检索，原文独立保留"),
            sa.Column("retrieval_text", sa.Text(), nullable=True, comment="含来源上下文及模型解释的检索文本"),
        ],
        "yp_knowledge_job": [
            sa.Column("processing_reserved_tokens", sa.Integer(), nullable=False, server_default="0", comment="跨重试加工输入和输出保守预留用量"),
            sa.Column("processing_tokens", sa.Integer(), nullable=True, comment="加工模型累计已报告 Token，未报告为空"),
            sa.Column("processing_call_count", sa.Integer(), nullable=False, server_default="0", comment="已发起文档加工模型调用数，含失败请求"),
            sa.Column("processing_estimated_cost", sa.Numeric(18, 6), nullable=True, comment="加工模型累计已报告用量的估算费用"),
            sa.Column("processing_usage", sa.JSON(), nullable=False, server_default=sa.text("'[]'"), comment="加工模型逐次用量、币种和耗时，不保存提示与响应"),
        ],
    }


def upgrade() -> None:
    """保留既有索引和精确原文偏移，新增字段回填兼容默认值。"""
    for table, columns in _columns().items():
        for column in columns:
            op.add_column(table, column)
    op.create_table(
        "yp_knowledge_enrichment_cache",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False, comment="主键 ID"),
        sa.Column("document_version_id", sa.Integer(), nullable=False, comment="不可变原始文档版本 ID"),
        sa.Column("fingerprint", sa.String(64), nullable=False, comment="证据批次、解析与模型身份及提示版本摘要"),
        sa.Column("notes", sa.JSON(), nullable=False, comment="已校验的片段位置到解释文本映射"),
        sa.Column("create_time", ShanghaiDateTime(), nullable=False, comment="创建时间"),
        sa.Column("update_time", ShanghaiDateTime(), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
        sa.UniqueConstraint("document_version_id", "fingerprint", name="uq_yp_knowledge_enrichment_version_key"),
        comment="知识文档加工结果缓存",
    )
    op.create_index("ix_yp_knowledge_enrichment_cache_document_version_id", "yp_knowledge_enrichment_cache", ["document_version_id"])


def downgrade() -> None:
    """只删除新增加工数据，既有原文片段、索引及活动指针保持有效。"""
    op.drop_table("yp_knowledge_enrichment_cache")
    for table, columns in reversed(list(_columns().items())):
        for column in reversed(columns):
            op.drop_column(table, column.name)
