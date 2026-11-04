"""refresh token reuse grace window

Revision ID: d4f8a1b6c3e9
Revises: c9a3e7d5f128
Create Date: 2026-11-03 09:00:00.000000

Additive: refresh_tokens.rotated_at (when a token was revoked BY ROTATION; NULL for every existing row, so existing tokens keep the
strict behaviour) and refresh_tokens.grace_replays (how many benign replays were answered with a sibling token).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a1b6c3e9"
down_revision: Union[str, Sequence[str], None] = "c9a3e7d5f128"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refresh_tokens", sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("refresh_tokens", sa.Column("grace_replays", sa.SmallInteger(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("refresh_tokens", "grace_replays")
    op.drop_column("refresh_tokens", "rotated_at")
