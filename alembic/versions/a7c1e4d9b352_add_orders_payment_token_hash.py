"""add orders.payment_token_hash (guest payment capability)

Revision ID: a7c1e4d9b352
Revises: f1c5d8b2a704
Create Date: 2026-10-23 12:00:00.000000

Additive and nullable: existing orders keep NULL (a guest order from before this cannot be paid by a guest capability, which
is the safe default). Only a SHA-256 hash of the capability is stored, never the capability itself. No data is rewritten.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c1e4d9b352"
down_revision: Union[str, Sequence[str], None] = "f1c5d8b2a704"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("payment_token_hash", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "payment_token_hash")
