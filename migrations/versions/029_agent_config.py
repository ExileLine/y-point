# -*- coding: utf-8 -*-
# @Time    : 2026-10-26 11:00:31
# @Author  : yangyuexiong
# @File    : 029_agent_config.py

"""新增用户私有 Agent 配置表。

Revision ID: 029
Revises: 028
"""

import sqlalchemy as sa
from alembic import op

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建无外键的 Agent 配置表及所有者索引。"""
    op.create_table(
        "yp_agent_config",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="ID"),
        sa.Column("owner_id", sa.Integer(), nullable=False, comment="智能体所属用户 ID"),
        sa.Column("name", sa.String(128), nullable=False, comment="智能体名称"),
        sa.Column("description", sa.String(1024), comment="智能体描述"),
        sa.Column("icon", sa.String(128), comment="智能体图标标识"),
        sa.Column("system_prompt", sa.Text(), nullable=False, comment="系统指令"),
        sa.Column("welcome_message", sa.String(2048), comment="欢迎语"),
        sa.Column("default_model_id", sa.Integer(), nullable=False, comment="默认模型 ID"),
        sa.Column("temperature", sa.Numeric(3, 2), nullable=False, comment="生成温度"),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False, comment="单次最大输出 Token 数"),
        sa.Column("enabled", sa.Boolean(), nullable=False, comment="是否启用"),
        sa.Column("version", sa.Integer(), nullable=False, comment="配置版本号"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), comment="状态(通用字段)"),
        comment="用户智能体配置表",
    )
    op.create_index("ix_yp_agent_config_owner_id", "yp_agent_config", ["owner_id"])


def downgrade() -> None:
    """移除 Agent 配置表。"""
    op.drop_index("ix_yp_agent_config_owner_id", table_name="yp_agent_config")
    op.drop_table("yp_agent_config")
