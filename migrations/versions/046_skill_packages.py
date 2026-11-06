# -*- coding: utf-8 -*-
# @Time    : 2026-11-06 11:15:34
# @Author  : yangyuexiong
# @File    : 046_skill_packages.py

"""增加 Skill 草稿、完整发布文件和受管脚本，不改写历史发布版本。"""

from alembic import op
import sqlalchemy as sa

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def _audit_columns() -> list:
    """统一新增表的审计字段及中文注释。"""
    return [
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True, comment="主键 ID"),
        sa.Column("create_time", sa.DateTime(timezone=True), nullable=False, comment="创建时间"),
        sa.Column("update_time", sa.DateTime(timezone=True), nullable=False, comment="更新时间"),
        sa.Column("create_timestamp", sa.BigInteger(), nullable=False, comment="创建时间戳"),
        sa.Column("update_timestamp", sa.BigInteger(), nullable=False, comment="更新时间戳"),
        sa.Column("is_deleted", sa.BigInteger(), nullable=True, comment="逻辑删除标识"),
        sa.Column("status", sa.BigInteger(), nullable=True, comment="状态(通用字段)"),
    ]


def upgrade() -> None:
    """创建工作区和精确包关联，既有文字包在打开草稿时按需转换。"""
    op.add_column("yp_user_tool", sa.Column("skill_id", sa.Integer(), nullable=True, comment="所属 Skill ID，非空表示包内托管脚本"))
    op.create_index("ix_yp_user_tool_skill_id", "yp_user_tool", ["skill_id"])
    op.create_table(
        "yp_skill_draft",
        *_audit_columns(),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="所属 Skill ID"),
        sa.Column("revision", sa.Integer(), nullable=False, comment="草稿并发修订号"),
        sa.Column("base_version", sa.Integer(), nullable=False, comment="编辑基于的当前发布版本"),
        sa.Column("files", sa.JSON(), nullable=False, comment="逻辑路径到不可变文件资源的映射"),
        sa.Column("settings", sa.JSON(), nullable=False, comment="待发布的分组可见性和启用设置"),
        sa.UniqueConstraint("skill_id", name="uq_yp_skill_draft_skill"),
        comment="Skill 文件包编辑草稿表",
    )
    op.create_table(
        "yp_skill_version_file",
        *_audit_columns(),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="所属 Skill ID"),
        sa.Column("skill_version", sa.Integer(), nullable=False, comment="所属发布版本"),
        sa.Column("path", sa.String(255), nullable=False, comment="包内规范化相对文件路径"),
        sa.Column("artifact_id", sa.String(36), nullable=False, comment="不可变私有文件资源 ID"),
        sa.Column("sha256", sa.String(64), nullable=False, comment="发布时固定的文件摘要"),
        sa.UniqueConstraint("skill_id", "skill_version", "path", name="uq_yp_skill_version_file_path"),
        comment="Skill 发布版本文件关联表",
    )
    op.create_index("ix_yp_skill_version_file_skill_id", "yp_skill_version_file", ["skill_id"])
    op.create_table(
        "yp_skill_script",
        *_audit_columns(),
        sa.Column("skill_id", sa.Integer(), nullable=False, comment="所属 Skill ID"),
        sa.Column("package_hash", sa.String(64), nullable=False, comment="试运行时固定的完整包摘要"),
        sa.Column("path", sa.String(255), nullable=False, comment="包内脚本路径"),
        sa.Column("tool_id", sa.Integer(), nullable=False, comment="托管的内部 Tool ID"),
        sa.Column("tool_version", sa.Integer(), nullable=False, comment="固定的内部 Tool 版本"),
        sa.UniqueConstraint("skill_id", "package_hash", "path", name="uq_yp_skill_script_package_path"),
        comment="Skill 文件包脚本执行绑定表",
    )
    op.create_index("ix_yp_skill_script_skill_id", "yp_skill_script", ["skill_id"])
    op.create_index("ix_yp_skill_script_tool_id", "yp_skill_script", ["tool_id"])


def downgrade() -> None:
    """存在包数据时拒绝降级，防止丢失草稿、发布资源及脚本审计关联。"""
    connection = op.get_bind()
    for name in ("yp_skill_draft", "yp_skill_version_file", "yp_skill_script"):
        table = sa.table(name, sa.column("id"))
        if connection.execute(sa.select(table.c.id).limit(1)).first() is not None:
            raise RuntimeError("存在 Skill 文件包数据，不能无损降级为 045；请保留 046 或恢复升级前备份")
    for name in ("yp_skill_script", "yp_skill_version_file", "yp_skill_draft"):
        op.drop_table(name)
    op.drop_index("ix_yp_user_tool_skill_id", table_name="yp_user_tool")
    with op.batch_alter_table("yp_user_tool") as batch:
        batch.drop_column("skill_id")
