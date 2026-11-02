# -*- coding: utf-8 -*-
# @Time    : 2026-11-02 22:33:20
# @Author  : yangyuexiong
# @File    : 040_tool_environments.py

"""增加用户脚本依赖清单与持久化环境准备状态，不改变历史 Tool 版本摘要。"""

from alembic import op
import sqlalchemy as sa

from app.models.base import ShanghaiDateTime

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为历史版本回填无额外依赖状态，再建立非空约束。"""
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.add_column(sa.Column("requirements", sa.JSON(), nullable=True, comment="用户声明的 Python 依赖清单"))
        batch.add_column(sa.Column("environment_state", sa.String(24), nullable=True, comment="脚本依赖环境准备状态"))
        batch.add_column(sa.Column("environment_spec_hash", sa.String(64), nullable=True, comment="依赖声明与基础镜像的准备请求摘要"))
        batch.add_column(sa.Column("environment_key", sa.String(64), nullable=True, comment="依赖与基础镜像的环境缓存键"))
        batch.add_column(sa.Column("environment_image", sa.String(128), nullable=True, comment="已固定的沙箱基础镜像 ID"))
        batch.add_column(sa.Column("environment_lock", sa.JSON(), nullable=True, comment="完整依赖版本和 wheel SHA256 清单"))
        batch.add_column(sa.Column("environment_manifest", sa.JSON(), nullable=True, comment="已安装环境文件的路径与 SHA256 清单"))
        batch.add_column(sa.Column("environment_error_code", sa.String(48), nullable=True, comment="环境准备脱敏失败类别"))
        batch.add_column(sa.Column("environment_error_detail", sa.String(256), nullable=True, comment="可公开的缺失依赖说明"))
        batch.add_column(sa.Column("environment_lease_token", sa.String(36), nullable=True, comment="环境准备任务租约令牌"))
        batch.add_column(sa.Column("environment_lease_expires_at", ShanghaiDateTime(), nullable=True, comment="环境准备租约到期时间"))
        batch.add_column(sa.Column("environment_attempts", sa.Integer(), nullable=True, comment="环境准备尝试次数"))
    versions = sa.table(
        "yp_user_tool_version", sa.column("requirements", sa.JSON()), sa.column("environment_state"), sa.column("environment_attempts")
    )
    op.execute(versions.update().values(requirements=[], environment_state="not_required", environment_attempts=0))
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.alter_column("requirements", existing_type=sa.JSON(), nullable=False)
        batch.alter_column("environment_state", existing_type=sa.String(24), nullable=False)
        batch.alter_column("environment_attempts", existing_type=sa.Integer(), nullable=False)
        batch.create_index("ix_yp_user_tool_version_environment_spec_hash", ["environment_spec_hash"])
        batch.create_index("ix_yp_user_tool_version_environment_key", ["environment_key"])


def downgrade() -> None:
    """移除依赖环境字段，保留原脚本和 Tool 版本数据。"""
    with op.batch_alter_table("yp_user_tool_version") as batch:
        batch.drop_index("ix_yp_user_tool_version_environment_spec_hash")
        batch.drop_index("ix_yp_user_tool_version_environment_key")
        for name in (
            "requirements",
            "environment_state",
            "environment_spec_hash",
            "environment_key",
            "environment_image",
            "environment_lock",
            "environment_manifest",
            "environment_error_code",
            "environment_error_detail",
            "environment_lease_token",
            "environment_lease_expires_at",
            "environment_attempts",
        ):
            batch.drop_column(name)
