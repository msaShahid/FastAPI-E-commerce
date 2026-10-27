"""email provider configurations (admin-selectable email service, credentials encrypted)

Revision ID: f5a9d3c7e124
Revises: e8c4a1f7b693
Create Date: 2026-10-27 15:00:00.000000

Additive: a new table, no existing data touched. At most one row can be active (partial unique index).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5a9d3c7e124"
down_revision: Union[str, Sequence[str], None] = "e8c4a1f7b693"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_provider_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=True),
        sa.Column("credentials_ciphertext", sa.Text(), nullable=False),
        sa.Column("username_hint", sa.String(length=100), nullable=False),
        sa.Column("password_hint", sa.String(length=12), nullable=False),
        sa.Column("from_email", sa.String(length=255), nullable=False),
        sa.Column("from_name", sa.String(length=100), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.String(length=40), nullable=True),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=40), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("provider IN ('google', 'zoho', 'mailtrap')", name="ck_email_provider_configs_provider"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", name="uq_email_provider_configs_provider"),
    )
    op.create_index(
        "ux_email_provider_configs_one_active", "email_provider_configs", ["is_active"], unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("ux_email_provider_configs_one_active", table_name="email_provider_configs", postgresql_where=sa.text("is_active"))
    op.drop_table("email_provider_configs")
