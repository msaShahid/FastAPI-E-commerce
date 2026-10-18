"""add orders.tax_snapshot (how each order's tax was decided)

Revision ID: c3d8a2e6f914
Revises: b8d4f1a6c230
Create Date: 2026-10-17 20:30:00.000000

Nullable JSONB, no backfill: existing orders keep NULL; their tax_cents is unchanged and
remains the record of what was charged.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c3d8a2e6f914"
down_revision: Union[str, Sequence[str], None] = "b8d4f1a6c230"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("tax_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "tax_snapshot")
