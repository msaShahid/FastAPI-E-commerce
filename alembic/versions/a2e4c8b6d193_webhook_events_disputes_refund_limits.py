"""webhook event states, refund cumulative limits, disputes

Revision ID: a2e4c8b6d193
Revises: f9c3b6e1d752
Create Date: 2026-10-18 15:00:00.000000

Additive/compatible. processed_webhook_events gains provider, event_type, status (existing rows become
'processed'), attempts and last_error, and its key becomes (provider, event_id). refunds gains `origin`
and a cumulative-limit trigger; the one-active-refund index is replaced by a one-IN-FLIGHT index for refunds
we started. A new empty `disputes` table is added. Nothing is rewritten or deleted.
The function texts must stay identical to app/modules/payments/models/provider_guard.py (a test checks).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2e4c8b6d193"
down_revision: Union[str, Sequence[str], None] = "f9c3b6e1d752"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

REFUNDS_CUMULATIVE_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION refunds_cumulative_guard() RETURNS trigger AS $$
DECLARE
    payment_amount integer;
    committed bigint;
BEGIN
    IF NEW.status NOT IN ('requested', 'pending', 'succeeded', 'uncertain') THEN
        RETURN NEW;
    END IF;
    SELECT amount_cents INTO payment_amount FROM payments WHERE id = NEW.payment_id FOR UPDATE;
    SELECT COALESCE(SUM(amount_cents), 0) INTO committed FROM refunds
        WHERE payment_id = NEW.payment_id AND id <> NEW.id
          AND status IN ('requested', 'pending', 'succeeded', 'uncertain');
    IF committed + NEW.amount_cents > payment_amount THEN
        RAISE EXCEPTION 'refunds of a payment cannot exceed the payment amount'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

DISPUTES_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION disputes_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.payment_id IS DISTINCT FROM OLD.payment_id
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.provider_dispute_id IS DISTINCT FROM OLD.provider_dispute_id THEN
        RAISE EXCEPTION 'what a dispute is about cannot be changed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('won', 'lost', 'closed') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'a % dispute is final', OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''


def upgrade() -> None:
    # --- webhook events: provider-scoped key, outcome and attempts
    op.add_column("processed_webhook_events", sa.Column("provider", sa.String(length=30), server_default="stripe", nullable=False))
    op.add_column("processed_webhook_events", sa.Column("event_type", sa.String(length=100), nullable=True))
    op.add_column("processed_webhook_events", sa.Column("status", sa.String(length=20), server_default="processed", nullable=False))
    op.add_column("processed_webhook_events", sa.Column("attempts", sa.Integer(), server_default="1", nullable=False))
    op.add_column("processed_webhook_events", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column("processed_webhook_events", sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint("processed_webhook_events_pkey", "processed_webhook_events", type_="primary")
    op.create_primary_key("processed_webhook_events_pkey", "processed_webhook_events", ["provider", "event_id"])
    op.create_check_constraint(
        "ck_webhook_events_status", "processed_webhook_events",
        "status IN ('processing', 'processed', 'ignored', 'failed')",
    )

    # --- refunds: origin, in-flight uniqueness, cumulative limit
    op.add_column("refunds", sa.Column("origin", sa.String(length=20), server_default="requested", nullable=False))
    op.create_check_constraint("ck_refunds_origin", "refunds", "origin IN ('requested', 'provider')")
    op.drop_index("ux_refunds_one_active_per_payment", table_name="refunds")
    op.create_index(
        "ux_refunds_one_inflight_per_payment", "refunds", ["payment_id"], unique=True,
        postgresql_where=sa.text("origin = 'requested' AND status IN ('requested', 'pending', 'uncertain')"),
    )

    # --- disputes
    op.create_table(
        "disputes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=30), nullable=False),
        sa.Column("provider_dispute_id", sa.String(length=255), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("provider_status", sa.String(length=50), nullable=True),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "provider_dispute_id", name="uq_disputes_provider_id"),
        sa.CheckConstraint("status IN ('open', 'under_review', 'won', 'lost', 'closed')", name="ck_disputes_status"),
        sa.CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_disputes_currency_format"),
    )
    op.create_index(op.f("ix_disputes_payment_id"), "disputes", ["payment_id"])
    op.create_index(op.f("ix_disputes_order_id"), "disputes", ["order_id"])
    op.create_index(op.f("ix_disputes_status"), "disputes", ["status"])

    op.execute(REFUNDS_CUMULATIVE_GUARD_SQL)
    op.execute(DISPUTES_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER trg_refunds_cumulative_guard BEFORE INSERT OR UPDATE "
        "ON refunds FOR EACH ROW EXECUTE FUNCTION refunds_cumulative_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_disputes_guard BEFORE UPDATE "
        "ON disputes FOR EACH ROW EXECUTE FUNCTION disputes_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_disputes_guard ON disputes")
    op.execute("DROP TRIGGER IF EXISTS trg_refunds_cumulative_guard ON refunds")
    op.execute("DROP FUNCTION IF EXISTS disputes_guard()")
    op.execute("DROP FUNCTION IF EXISTS refunds_cumulative_guard()")
    op.drop_index(op.f("ix_disputes_status"), table_name="disputes")
    op.drop_index(op.f("ix_disputes_order_id"), table_name="disputes")
    op.drop_index(op.f("ix_disputes_payment_id"), table_name="disputes")
    op.drop_table("disputes")
    op.drop_index("ux_refunds_one_inflight_per_payment", table_name="refunds")
    op.create_index(
        "ux_refunds_one_active_per_payment", "refunds", ["payment_id"], unique=True,
        postgresql_where=sa.text("status IN ('requested', 'pending', 'succeeded', 'uncertain')"),
    )
    op.drop_constraint("ck_refunds_origin", "refunds", type_="check")
    op.drop_column("refunds", "origin")
    op.drop_constraint("ck_webhook_events_status", "processed_webhook_events", type_="check")
    op.drop_constraint("processed_webhook_events_pkey", "processed_webhook_events", type_="primary")
    op.create_primary_key("processed_webhook_events_pkey", "processed_webhook_events", ["event_id"])
    for column in ("processed_at", "last_error", "attempts", "status", "event_type", "provider"):
        op.drop_column("processed_webhook_events", column)
