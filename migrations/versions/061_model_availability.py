"""保存模型实际访问故障，与能力标签分开。

Revision ID: 061
Revises: 060
"""

from alembic import op
import sqlalchemy as sa

revision = '061'
down_revision = '060'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """未知状态不阻断已有模型；通过实际调用或管理员测试记录故障。"""
    with op.batch_alter_table('yp_model_catalog') as batch:
        batch.add_column(sa.Column('availability_failure', sa.String(32), nullable=True, comment='最近确认的模型访问故障，空值表示未被阻断'))
        batch.add_column(sa.Column('availability_checked_at', sa.DateTime(timezone=True), nullable=True, comment='最近模型访问状态核验时间'))


def downgrade() -> None:
    """删除访问状态，不修改历史能力验证和调用记录。"""
    with op.batch_alter_table('yp_model_catalog') as batch:
        batch.drop_column('availability_checked_at')
        batch.drop_column('availability_failure')
