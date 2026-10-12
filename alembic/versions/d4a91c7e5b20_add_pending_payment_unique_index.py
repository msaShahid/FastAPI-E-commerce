"""add partial unique index: one pending payment per order

Revision ID: d4a91c7e5b20
Revises: b7d2f4a83e19
Create Date: 2026-10-12 19:30:00.000000

The Payment model declares ux_payments_order_id_pending (at most one
"pending" payment per order -- the database-level backstop behind the
application's SELECT ... FOR UPDATE guard), but some databases were built
without it, so `alembic check` reported it as missing.

IF NOT EXISTS / IF EXISTS make this safe on every database: it creates the
index where it is missing and does nothing where it already exists.

If this fails with "could not create unique index ... is duplicated", the
database already contains two pending payments for one order. Resolve those
rows first (cancel or delete the extra pending payment), then re-run.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4a91c7e5b20'
down_revision: Union[str, Sequence[str], None] = 'b7d2f4a83e19'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_payments_order_id_pending "
        "ON payments (order_id) WHERE status = 'pending'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ux_payments_order_id_pending")
