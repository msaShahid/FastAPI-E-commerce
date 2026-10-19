"""order/payment lifecycle integrity: refunds, reconciliations, stock release, guards

Revision ID: f9c3b6e1d752
Revises: e5b7d2a9c418
Create Date: 2026-10-18 12:00:00.000000

Additive. Adds orders.stock_released_at (NULL; existing orders are untouched), order history
`reason`, the (empty) refunds and payment_reconciliations tables, and two guard triggers. The
orders guard only evaluates FUTURE status changes: it rewrites no existing row.
The function texts must stay identical to app/modules/payments/models/lifecycle_guard.py (a test checks).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f9c3b6e1d752"
down_revision: Union[str, Sequence[str], None] = "e5b7d2a9c418"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORDERS_LIFECYCLE_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION orders_lifecycle_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.stock_released_at IS NOT NULL AND NEW.stock_released_at IS DISTINCT FROM OLD.stock_released_at THEN
        RAISE EXCEPTION 'stock release cannot be repeated or undone'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT ((OLD.status = 'pending' AND NEW.status IN ('paid', 'cancelled'))
                OR (OLD.status = 'paid' AND NEW.status IN ('fulfilled', 'refunded'))
                OR (OLD.status = 'fulfilled' AND NEW.status = 'refunded')) THEN
            RAISE EXCEPTION 'an order cannot change status from % to %', OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'paid'
           AND NOT EXISTS (SELECT 1 FROM payments WHERE order_id = NEW.id AND status = 'succeeded') THEN
            RAISE EXCEPTION 'an order cannot become paid without a succeeded payment'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'refunded'
           AND NOT EXISTS (SELECT 1 FROM refunds WHERE order_id = NEW.id AND status = 'succeeded') THEN
            RAISE EXCEPTION 'an order cannot become refunded without a succeeded refund'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'cancelled' AND NEW.stock_released_at IS NULL THEN
            RAISE EXCEPTION 'an order cannot be cancelled before its stock is released'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

REFUNDS_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION refunds_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.payment_id IS DISTINCT FROM OLD.payment_id
       OR NEW.order_id IS DISTINCT FROM OLD.order_id
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'what a refund is for cannot be changed after it is created'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'failed') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'a % refund is final', OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''


def upgrade() -> None:
    op.add_column("orders", sa.Column("stock_released_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("order_status_history", sa.Column("reason", sa.String(length=500), nullable=True))

    op.create_table(
        "refunds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="requested", nullable=False),
        sa.Column("provider_refund_id", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.CheckConstraint("amount_cents > 0", name="ck_refunds_amount_positive"),
        sa.CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_refunds_currency_format"),
        sa.CheckConstraint(
            "status IN ('requested', 'pending', 'succeeded', 'failed', 'uncertain')", name="ck_refunds_status"
        ),
    )
    op.create_index(op.f("ix_refunds_payment_id"), "refunds", ["payment_id"])
    op.create_index(op.f("ix_refunds_order_id"), "refunds", ["order_id"])
    op.create_index(
        "ux_refunds_one_active_per_payment",
        "refunds",
        ["payment_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('requested', 'pending', 'succeeded', 'uncertain')"),
    )

    op.create_table(
        "payment_reconciliations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("provider_payment_id", sa.String(length=255), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("resolution", sa.String(length=40), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["resolved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_reconciliations_status"),
        sa.CheckConstraint(
            "status = 'open' OR (resolved_at IS NOT NULL AND resolution IS NOT NULL "
            "AND resolution_note IS NOT NULL AND length(btrim(resolution_note)) > 0)",
            name="ck_reconciliations_resolved_fields",
        ),
    )
    op.create_index(op.f("ix_payment_reconciliations_kind"), "payment_reconciliations", ["kind"])
    op.create_index(op.f("ix_payment_reconciliations_status"), "payment_reconciliations", ["status"])

    op.execute(ORDERS_LIFECYCLE_GUARD_SQL)
    op.execute(REFUNDS_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER trg_orders_lifecycle_guard BEFORE UPDATE "
        "ON orders FOR EACH ROW EXECUTE FUNCTION orders_lifecycle_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_refunds_guard BEFORE UPDATE "
        "ON refunds FOR EACH ROW EXECUTE FUNCTION refunds_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_refunds_guard ON refunds")
    op.execute("DROP TRIGGER IF EXISTS trg_orders_lifecycle_guard ON orders")
    op.execute("DROP FUNCTION IF EXISTS refunds_guard()")
    op.execute("DROP FUNCTION IF EXISTS orders_lifecycle_guard()")
    op.drop_index(op.f("ix_payment_reconciliations_status"), table_name="payment_reconciliations")
    op.drop_index(op.f("ix_payment_reconciliations_kind"), table_name="payment_reconciliations")
    op.drop_table("payment_reconciliations")
    op.drop_index("ux_refunds_one_active_per_payment", table_name="refunds")
    op.drop_index(op.f("ix_refunds_order_id"), table_name="refunds")
    op.drop_index(op.f("ix_refunds_payment_id"), table_name="refunds")
    op.drop_table("refunds")
    op.drop_column("order_status_history", "reason")
    op.drop_column("orders", "stock_released_at")
