"""add orders.shipping_snapshot (shipping policy used for each order)

Revision ID: d5f1b2c9a604
Revises: c4e8a1d7f203
Create Date: 2026-10-17 17:00:00.000000

Nullable JSONB, no backfill: existing orders keep NULL (their shipping_cents is
unchanged and remains the record of what was charged).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5f1b2c9a604"
down_revision: Union[str, Sequence[str], None] = "c4e8a1d7f203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("shipping_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "shipping_snapshot")
