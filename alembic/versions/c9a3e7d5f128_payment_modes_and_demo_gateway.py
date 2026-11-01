"""payment modes, the built-in demo gateway and gateway checks

Revision ID: c9a3e7d5f128
Revises: b5e9c3a7d216
Create Date: 2026-11-01 14:00:00.000000

Additive: the payment_settings table (live payments start OFF), the demo gateway's intent table, last-check columns on payment_provider_configs,
orders.paid_in_demo_mode (false for every existing order) and a rule that the demo provider can only ever be a TEST-mode configuration.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9a3e7d5f128"
down_revision: Union[str, Sequence[str], None] = "b5e9c3a7d216"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_settings",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("live_payments_active", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("live_activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("live_activated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("live_activated_config_id", sa.Uuid(), nullable=True),
        sa.Column("last_deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_payment_settings_single_row"),
        sa.ForeignKeyConstraint(["live_activated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["live_activated_config_id"], ["payment_provider_configs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "demo_payment_intents",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="requires_payment_method", nullable=False),
        sa.Column("last_failure_code", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('requires_payment_method', 'succeeded', 'canceled')", name="ck_demo_payment_intents_status"),
        sa.CheckConstraint("amount_cents > 0", name="ck_demo_payment_intents_amount"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column("payment_provider_configs", sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payment_provider_configs", sa.Column("last_check_ok", sa.Boolean(), nullable=True))
    op.add_column("payment_provider_configs", sa.Column("last_check_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_check_constraint("ck_provider_configs_demo_is_test", "payment_provider_configs", "provider <> 'demo' OR mode = 'test'")
    op.add_column("orders", sa.Column("paid_in_demo_mode", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("orders", "paid_in_demo_mode")
    op.drop_constraint("ck_provider_configs_demo_is_test", "payment_provider_configs", type_="check")
    for column in ("last_check_result", "last_check_ok", "last_check_at"):
        op.drop_column("payment_provider_configs", column)
    op.drop_table("demo_payment_intents")
    op.drop_table("payment_settings")
