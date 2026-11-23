# -*- coding: utf-8 -*-
# @Time    : 2026-11-23
# @Author  : Codex
# @File    : 066_mcp_budgets_results.py

"""保存脱敏请求预算与 MCP 结果来源，正文复用私有产物存储。"""

from alembic import op
import sqlalchemy as sa

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史请求和产物保持空值，不猜测完整性或补写来源。"""
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.add_column(sa.Column("context_budget", sa.JSON(none_as_null=True), nullable=True, comment="最后模型请求的脱敏上下文预算分解"))
    with op.batch_alter_table("yp_artifact") as batch:
        batch.add_column(sa.Column("tool_result", sa.JSON(none_as_null=True), nullable=True, comment="MCP 单次响应的脱敏结果范围与调用来源"))


def downgrade() -> None:
    """保留新交付语义，禁止将已有结果误认为模型生成文件。"""
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM yp_artifact WHERE tool_result IS NOT NULL")).scalar_one():
        raise RuntimeError("仍有 MCP 结果来源，请先备份并迁出后回退")
    with op.batch_alter_table("yp_artifact") as batch:
        batch.drop_column("tool_result")
    with op.batch_alter_table("yp_agent_run") as batch:
        batch.drop_column("context_budget")
