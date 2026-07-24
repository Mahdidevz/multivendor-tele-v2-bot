"""add_discount_redirect_partner_fields

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-24 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1) Vendor: redirect_target_id ( nullable, FK to vendors.id )
    op.add_column(
        'vendors',
        sa.Column('redirect_target_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_vendors_redirect_target_id',
        'vendors',
        'vendors',
        ['redirect_target_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # 2) Transaction: discount_percent, original_amount, origin_vendor_id
    op.add_column(
        'transactions',
        sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'transactions',
        sa.Column('original_amount', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column(
        'transactions',
        sa.Column('origin_vendor_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_transactions_origin_vendor_id',
        'transactions',
        'vendors',
        ['origin_vendor_id'],
        ['id'],
        ondelete='SET NULL',
    )

    # 3) DiscountCode table
    op.create_table(
        'discount_codes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=50), nullable=False),
        sa.Column('discount_percent', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vendor_id', 'code', name='uq_discount_vendor_code'),
    )
    op.create_index('ix_discount_codes_vendor_id', 'discount_codes', ['vendor_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_discount_codes_vendor_id', table_name='discount_codes')
    op.drop_table('discount_codes')
    op.drop_constraint('fk_transactions_origin_vendor_id', 'transactions', type_='foreignkey')
    op.drop_column('transactions', 'origin_vendor_id')
    op.drop_column('transactions', 'original_amount')
    op.drop_column('transactions', 'discount_percent')
    op.drop_constraint('fk_vendors_redirect_target_id', 'vendors', type_='foreignkey')
    op.drop_column('vendors', 'redirect_target_id')
