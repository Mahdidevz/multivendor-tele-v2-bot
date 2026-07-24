"""add_force_join_and_test_flag

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
Create Date: 2026-07-24 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1) پرچم دریافت تست رایگان برای کاربران
    op.add_column(
        'users',
        sa.Column('has_received_test', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )

    # 2) جدول کانالهای عضویت اجباری هر فروشنده (Force Join)
    op.create_table(
        'force_join_channels',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('chat_id', sa.String(length=100), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('url', sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_force_join_channels_vendor_id',
        'force_join_channels',
        ['vendor_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_force_join_channels_vendor_id', table_name='force_join_channels')
    op.drop_table('force_join_channels')
    op.drop_column('users', 'has_received_test')
