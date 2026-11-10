# -*- coding: utf-8 -*-
# @Time    : 2026-11-03 11:48:47
# @Author  : yangyuexiong
# @File    : knowledge.py

"""知识库管理、索引任务和有界检索的输入契约。"""

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.enums import KnowledgeRerank, KnowledgeSearchMode

KnowledgeName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class KnowledgeCreateIn(BaseModel):
    """创建私有知识库，不自动共享给 Agent 的使用者。"""

    model_config = ConfigDict(extra="forbid")
    name: KnowledgeName = Field(description="知识库名称", examples=["员工手册"])
    description: str | None = Field(default=None, max_length=1024, description="知识库用途", examples=["差旅及报销制度"])
    search_mode: KnowledgeSearchMode = Field(default=KnowledgeSearchMode.KEYWORD, description="关键词或混合检索策略", examples=["keyword"])
    embedding_model_id: int | None = Field(default=None, gt=0, description="独立验证通过的嵌入模型 ID", examples=[2])
    chunk_size: int = Field(default=800, ge=200, le=2000, description="每个片段的最大字符数", examples=[800])
    chunk_overlap: int = Field(default=100, ge=0, le=500, description="相邻片段重叠字符数，须小于块大小的一半", examples=[100])
    embedding_token_budget: int = Field(default=200000, ge=1, le=2000000, description="单次索引嵌入输入预算", examples=[200000])
    embedding_cost_budget: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=18,
        decimal_places=6,
        description="模型配置币种下的单次嵌入估算成本上限",
        examples=["0.100000"],
    )

    @model_validator(mode="after")
    def validate_configuration(self):
        """联合校验分块重叠及混合检索模型引用。"""
        if self.chunk_overlap >= self.chunk_size // 2:
            raise ValueError("片段重叠须小于块大小的一半")
        if self.search_mode == KnowledgeSearchMode.HYBRID and self.embedding_model_id is None:
            raise ValueError("混合检索必须指定嵌入模型")
        return self


class KnowledgeUpdateIn(KnowledgeCreateIn):
    """使用完整配置和期望版本更新，产生全新索引代。"""

    expected_revision: int = Field(ge=1, description="读取到的知识库配置版本", examples=[1])


class KnowledgeRevisionIn(BaseModel):
    """重建和删除时防止覆盖并发更新。"""

    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, description="读取到的知识库配置版本", examples=[2])


class KnowledgeShareIn(BaseModel):
    """仅授予指定用户只读权限。"""

    model_config = ConfigDict(extra="forbid")
    user_id: int = Field(gt=0, description="接受只读共享的有效用户 ID", examples=[2])


class KnowledgeSearchIn(BaseModel):
    """同时限制检索数量、片段预算和可选重排。"""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=512, description="检索问题，不得仅为空白", examples=["差旅报销提交期限"])
    top_k: int = Field(default=5, ge=1, le=10, description="最大返回片段数", examples=[3])
    max_bytes: int = Field(default=12000, ge=512, le=24000, description="返回片段正文的总 UTF-8 字节预算", examples=[12000])
    keyword_only: bool = Field(default=False, description="显式只使用关键词，允许在嵌入服务故障时人工降级", examples=[False])
    min_similarity: float = Field(default=0.7, ge=0, le=1, description="向量候选最低余弦相似度", examples=[0.7])
    rerank: KnowledgeRerank = Field(default=KnowledgeRerank.NONE, description="可选词项覆盖重排，不调用外部模型", examples=["none"])
