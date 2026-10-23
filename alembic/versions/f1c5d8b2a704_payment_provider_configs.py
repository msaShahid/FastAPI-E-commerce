"""payment provider configurations (encrypted credentials) and per-payment provider linkage

Revision ID: f1c5d8b2a704
Revises: e9a2c6f4b811
Create Date: 2026-10-19 16:00:00.000000

Additive. Creates the (empty) payment_provider_configs table and two NULLABLE columns on payments. Existing payments keep
provider_config_id NULL, which means "created by the environment-configured Stripe": they keep working exactly as before, and no
configuration is created or enabled by this migration. Credentials are never stored here in plaintext.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1c5d8b2a704"
down_revision: Union[str, Sequence[str], None] = "e9a2c6f4b811"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_provider_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("mode", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("is_preferred", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("credentials_ciphertext", sa.Text(), nullable=True),
        sa.Column("credential_hint", sa.String(length=20), nullable=True),
        sa.Column("public_identifier", sa.String(length=100), nullable=True),
        sa.Column("credentials_status", sa.String(length=20), server_default="unvalidated", nullable=False),
        sa.Column("credentials_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("previous_webhook_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("webhook_hint", sa.String(length=20), nullable=True),
        sa.Column("webhook_status", sa.String(length=20), server_default="unverified", nullable=False),
        sa.Column("webhook_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("currencies", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("sandbox_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sandbox_verification_ref", sa.Text(), nullable=True),
        sa.Column("production_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("production_review_ref", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "mode", name="uq_provider_configs_provider_mode"),
        sa.CheckConstraint("provider ~ '^[a-z][a-z0-9_]{1,29}$'", name="ck_provider_configs_provider"),
        sa.CheckConstraint("mode IN ('test', 'live')", name="ck_provider_configs_mode"),
        sa.CheckConstraint("status IN ('draft', 'enabled', 'disabled')", name="ck_provider_configs_status"),
        sa.CheckConstraint("credentials_status IN ('unvalidated', 'valid', 'invalid')", name="ck_provider_configs_cred_status"),
        sa.CheckConstraint("webhook_status IN ('unverified', 'verified')", name="ck_provider_configs_webhook_status"),
        sa.CheckConstraint("NOT is_preferred OR status = 'enabled'", name="ck_provider_configs_preferred_enabled"),
        sa.CheckConstraint(
            "status <> 'enabled' OR (credentials_status = 'valid' AND credentials_ciphertext IS NOT NULL)",
            name="ck_provider_configs_enabled_valid",
        ),
        sa.CheckConstraint(
            "NOT (mode = 'live' AND status = 'enabled') OR (webhook_status = 'verified' "
            "AND sandbox_verified_at IS NOT NULL AND production_review_at IS NOT NULL)",
            name="ck_provider_configs_live_requirements",
        ),
    )
    op.create_index(
        "ux_provider_configs_one_preferred", "payment_provider_configs", ["is_preferred"], unique=True,
        postgresql_where=sa.text("is_preferred"),
    )
    op.add_column("payments", sa.Column("provider_config_id", sa.Uuid(), nullable=True))
    op.add_column("payments", sa.Column("provider_reference", sa.String(length=255), nullable=True))
    op.create_foreign_key(
        "fk_payments_provider_config_id", "payments", "payment_provider_configs", ["provider_config_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_index(op.f("ix_payments_provider_config_id"), "payments", ["provider_config_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_payments_provider_config_id"), table_name="payments")
    op.drop_constraint("fk_payments_provider_config_id", "payments", type_="foreignkey")
    op.drop_column("payments", "provider_reference")
    op.drop_column("payments", "provider_config_id")
    op.drop_index("ux_provider_configs_one_preferred", table_name="payment_provider_configs")
    op.drop_table("payment_provider_configs")
