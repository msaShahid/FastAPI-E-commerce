"""store currency, order currency, and money-stability guards

Revision ID: e5b7d2a9c418
Revises: d6a9c4f1b873
Create Date: 2026-10-18 10:00:00.000000

Additive. Creates the (empty) store_settings table, adds a NULLABLE orders.currency and format
checks. Existing orders keep currency NULL: their currency is unknown and is NOT assumed (their
payments, if any, keep the currency they were created with). Nothing is backfilled and no existing
data changes. Until an administrator sets the store currency, checkout answers 503
(store_currency_not_configured).
The function texts must stay identical to app/modules/store/models/guard.py (a test checks).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b7d2a9c418"
down_revision: Union[str, Sequence[str], None] = "d6a9c4f1b873"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STORE_CURRENCY_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION store_currency_guard() RETURNS trigger AS $$
BEGIN
    IF (TG_OP = 'DELETE' OR NEW.currency IS DISTINCT FROM OLD.currency)
       AND EXISTS (SELECT 1 FROM orders) THEN
        RAISE EXCEPTION 'store currency cannot be changed once orders exist'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

ORDER_CURRENCY_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION orders_currency_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.currency IS NOT NULL AND NEW.currency IS DISTINCT FROM OLD.currency THEN
        RAISE EXCEPTION 'an order currency cannot be changed once set'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

PAYMENT_AMOUNT_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION payments_amount_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.currency IS DISTINCT FROM OLD.currency OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents THEN
        RAISE EXCEPTION 'a payment amount and currency cannot be changed after creation'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''


def upgrade() -> None:
    op.create_table(
        "store_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_store_settings_singleton"),
        sa.CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_store_settings_currency_format"),
    )
    op.add_column("orders", sa.Column("currency", sa.String(length=3), nullable=True))
    op.create_check_constraint(
        "ck_orders_currency_format", "orders", "currency IS NULL OR currency ~ '^[a-z]{3}$'"
    )
    op.create_check_constraint("ck_payments_currency_format", "payments", "currency ~ '^[a-z]{3}$'")

    op.execute(STORE_CURRENCY_GUARD_SQL)
    op.execute(ORDER_CURRENCY_GUARD_SQL)
    op.execute(PAYMENT_AMOUNT_GUARD_SQL)
    op.execute(
        "CREATE TRIGGER trg_store_settings_currency_guard BEFORE UPDATE OR DELETE "
        "ON store_settings FOR EACH ROW EXECUTE FUNCTION store_currency_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_orders_currency_guard BEFORE UPDATE OF currency "
        "ON orders FOR EACH ROW EXECUTE FUNCTION orders_currency_guard()"
    )
    op.execute(
        "CREATE TRIGGER trg_payments_amount_guard BEFORE UPDATE OF currency, amount_cents "
        "ON payments FOR EACH ROW EXECUTE FUNCTION payments_amount_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_payments_amount_guard ON payments")
    op.execute("DROP TRIGGER IF EXISTS trg_orders_currency_guard ON orders")
    op.execute("DROP TRIGGER IF EXISTS trg_store_settings_currency_guard ON store_settings")
    op.execute("DROP FUNCTION IF EXISTS payments_amount_guard()")
    op.execute("DROP FUNCTION IF EXISTS orders_currency_guard()")
    op.execute("DROP FUNCTION IF EXISTS store_currency_guard()")
    op.drop_constraint("ck_payments_currency_format", "payments", type_="check")
    op.drop_constraint("ck_orders_currency_format", "orders", type_="check")
    op.drop_column("orders", "currency")
    op.drop_table("store_settings")
