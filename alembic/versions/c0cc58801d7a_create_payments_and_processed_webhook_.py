"""create payments and processed_webhook_events tables

Revision ID: c0cc58801d7a
Revises: 087770ba3661
Create Date: 2026-09-28 14:10:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c0cc58801d7a'
down_revision: str | Sequence[str] | None = '087770ba3661'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'payments',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('order_id', sa.Uuid(), nullable=False),
        sa.Column('provider', sa.String(length=50), server_default='stripe', nullable=False),
        sa.Column('provider_payment_id', sa.String(length=255), nullable=False),
        sa.Column(
            'status',
            sa.Enum('pending', 'succeeded', 'failed', 'refunded', name='payment_status'),
            server_default='pending',
            nullable=False,
        ),
        sa.Column('amount_cents', sa.Integer(), nullable=False),
        sa.Column('currency', sa.String(length=3), server_default='usd', nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('amount_cents > 0', name='ck_payments_amount_positive'),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_payments_order_id'), 'payments', ['order_id'], unique=False
    )
    op.create_index(
        op.f('ix_payments_provider_payment_id'),
        'payments',
        ['provider_payment_id'],
        unique=True,
    )
    # Backstop against two concurrent requests both creating a "pending"
    # payment for the same order (the application also guards this with
    # a SELECT ... FOR UPDATE on the order row -- this index is
    # defense-in-depth at the database level, not a substitute for it).
    op.create_index(
        'ux_payments_order_id_pending',
        'payments',
        ['order_id'],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        'processed_webhook_events',
        sa.Column('event_id', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('event_id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('processed_webhook_events')
    op.drop_index('ux_payments_order_id_pending', table_name='payments')
    op.drop_index(op.f('ix_payments_provider_payment_id'), table_name='payments')
    op.drop_index(op.f('ix_payments_order_id'), table_name='payments')
    op.drop_table('payments')
    # Note: this leaves the native `payment_status` enum type in place,
    # same as `order_status` / `product_status` do on their downgrades
    # elsewhere in this chain -- consistent with, not a fix for, that
    # existing behavior.
