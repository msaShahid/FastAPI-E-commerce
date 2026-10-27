"""account recovery: auth_tokens, and users.email_verified_at

Revision ID: e8c4a1f7b693
Revises: d7b3f1a9c524
Create Date: 2026-10-27 10:00:00.000000

Additive. `users.email_verified_at` is NULL for every existing account (nothing is assumed verified). `auth_tokens` holds only
the SHA-256 of each emailed secret. No existing data is changed.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e8c4a1f7b693"
down_revision: Union[str, Sequence[str], None] = "d7b3f1a9c524"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=30), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("purpose IN ('password_reset', 'email_verification', 'email_change')", name="ck_auth_tokens_purpose"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_auth_tokens_user_purpose_created", "auth_tokens", ["user_id", "purpose", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_tokens_user_purpose_created", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_column("users", "email_verified_at")
