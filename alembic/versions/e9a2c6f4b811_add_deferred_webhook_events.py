"""deferred webhook events: bounded recovery for events that arrive before their payment record

Revision ID: e9a2c6f4b811
Revises: d4b7e1a9c635
Create Date: 2026-10-19 11:00:00.000000

Additive: one new empty table. It stores only normalised identifiers/amount/currency/status of an event (never the raw
payload), with a status, an attempt counter, the next attempt time and an expiry, so recovery is bounded.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e9a2c6f4b811"
down_revision: Union[str, Sequence[str], None] = "d4b7e1a9c635"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deferred_webhook_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=True),
        sa.Column("amount_received", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("provider_refund_id", sa.String(length=255), nullable=True),
        sa.Column("refund_status", sa.String(length=50), nullable=True),
        sa.Column("provider_dispute_id", sa.String(length=255), nullable=True),
        sa.Column("dispute_status", sa.String(length=50), nullable=True),
        sa.Column("dispute_reason", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "event_id", name="uq_deferred_events_provider_event"),
        sa.CheckConstraint("status IN ('pending', 'recovered', 'expired')", name="ck_deferred_events_status"),
    )
    op.create_index("ix_deferred_events_due", "deferred_webhook_events", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_deferred_events_due", table_name="deferred_webhook_events")
    op.drop_table("deferred_webhook_events")
