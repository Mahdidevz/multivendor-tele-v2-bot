"""add_transaction_discount_code_id

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-24 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 🌟 [جدید] ارجاع تراکنش به کد تخفیف استفادهشده.
    # ondelete=SET NULL → اگر کد تخفیف حذف شد، سابقه مالی تراکنش (original_amount/discount_percent) حفظ میشود.
    op.add_column(
        'transactions',
        sa.Column('discount_code_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_transactions_discount_code_id',
        'transactions',
        'discount_codes',
        ['discount_code_id'],
        ['id'],  # فقط نام ستون؛ جدول مرجع در آرگومان قبلی مشخص شده است
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_transactions_discount_code_id',
        'transactions',
        ['discount_code_id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_transactions_discount_code_id', table_name='transactions')
    op.drop_constraint('fk_transactions_discount_code_id', 'transactions', type_='foreignkey')
    op.drop_column('transactions', 'discount_code_id')
