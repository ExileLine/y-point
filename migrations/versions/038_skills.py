# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 10:33:31
# @Author  : yangyuexiong
# @File    : 038_skills.py

"""新增 Skill 包、版本及 Agent/Run 关联。

Revision ID: 038
Revises: 037
"""

import sqlalchemy as sa
from alembic import op

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def _audit_columns():
    """生成与业务模型一致的通用审计列。"""
    return [
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """创建 Skill 的四张业务表及必要索引。"""
    op.create_table(
        "yp_skill",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="创建用户 ID"),
        sa.Column("name", sa.String(64), nullable=False, comment="Skill 唯一名称"),
        sa.Column("description", sa.String(1024), nullable=False, comment="Skill 简介"),
        sa.Column("category", sa.String(64), comment="分组名称"),
        sa.Column("visibility", sa.String(16), nullable=False, comment="私有或公开"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否启用"),
        sa.Column("current_version", sa.Integer(), nullable=False, comment="当前版本号"),
        *_audit_columns(),
        sa.UniqueConstraint("owner_id", "name", name="uq_yp_skill_owner_name"),
        comment="用户 Skill 表",
    )
    op.create_index("ix_yp_skill_owner_id", "yp_skill", ["owner_id"])
    op.create_table(
        "yp_skill_version",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="Skill ID"),
        sa.Column("version", sa.Integer(), nullable=False, comment="版本号"),
        sa.Column("content_hash", sa.String(64), nullable=False, comment="包内容 SHA256"),
        sa.Column("files", sa.JSON(), nullable=False, comment="UTF-8 文件路径与内容映射"),
        sa.Column("manifest", sa.JSON(), nullable=False, comment="文件清单"),
        *_audit_columns(),
        sa.UniqueConstraint("skill_id", "version", name="uq_yp_skill_version_number"),
        comment="Skill 不可变版本表",
    )
    op.create_index("ix_yp_skill_version_skill_id", "yp_skill_version", ["skill_id"])
    op.create_table(
        "yp_agent_skill",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("agent_id", sa.Integer(), nullable=False, comment="智能体 ID"),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="Skill ID"),
        sa.Column("skill_version", sa.Integer(), nullable=False, comment="固定 Skill 版本号"),
        *_audit_columns(),
        sa.UniqueConstraint("agent_id", "skill_id", name="uq_yp_agent_skill_pair"),
        comment="智能体关联 Skill 版本表",
    )
    op.create_index("ix_yp_agent_skill_agent_id", "yp_agent_skill", ["agent_id"])
    op.create_table(
        "yp_run_skill",
        sa.Column("id", sa.Integer(), primary_key=True, comment="主键 ID"),
        sa.Column("run_id", sa.String(36), nullable=False, comment="运行 ID"),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="Skill ID"),
        sa.Column("skill_version", sa.Integer(), nullable=False, comment="运行固定版本号"),
        *_audit_columns(),
        sa.UniqueConstraint("run_id", "skill_id", name="uq_yp_run_skill_pair"),
        comment="运行使用的 Skill 版本表",
    )
    op.create_index("ix_yp_run_skill_run_id", "yp_run_skill", ["run_id"])


def downgrade() -> None:
    """移除 Skill 相关业务表。"""
    for table, index in (
        ("yp_run_skill", "ix_yp_run_skill_run_id"),
        ("yp_agent_skill", "ix_yp_agent_skill_agent_id"),
        ("yp_skill_version", "ix_yp_skill_version_skill_id"),
        ("yp_skill", "ix_yp_skill_owner_id"),
    ):
        op.drop_index(index, table_name=table)
        op.drop_table(table)
