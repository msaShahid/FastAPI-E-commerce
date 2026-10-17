"""add orders.request_hash (idempotency request fingerprint)

Revision ID: c4e8a1d7f203
Revises: a8c3e5d7b1f4
Create Date: 2026-10-17 15:00:00.000000

Nullable on purpose: existing orders keep NULL, and replays of their
Idempotency-Key are accepted as before. No existing data is rewritten.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1d7f203"
down_revision: Union[str, Sequence[str], None] = "a8c3e5d7b1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("request_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "request_hash")
