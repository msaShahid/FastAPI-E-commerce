"""coupons and order discounts

Revision ID: a8d2f6c1e045
Revises: f1c4d8e2a937
Create Date: 2026-10-31 18:00:00.000000

Additive: the coupons table, discount columns on orders and order_items (defaulting to 0 / NULL, so every existing order keeps meaning
exactly what it did), and a trigger that freezes an order's discount once it exists. The function and trigger text is identical to
app/modules/coupons/models/guard.py (a test asserts it).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a8d2f6c1e045"
down_revision: Union[str, Sequence[str], None] = "f1c4d8e2a937"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = r'''CREATE OR REPLACE FUNCTION coupon_fields_frozen() RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'orders' THEN
        IF NEW.discount_cents IS DISTINCT FROM OLD.discount_cents OR NEW.coupon_id IS DISTINCT FROM OLD.coupon_id
           OR NEW.coupon_code IS DISTINCT FROM OLD.coupon_code OR NEW.coupon_snapshot IS DISTINCT FROM OLD.coupon_snapshot THEN
            RAISE EXCEPTION 'orders: the discount of an order cannot be changed' USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF NEW.discount_cents IS DISTINCT FROM OLD.discount_cents THEN
        RAISE EXCEPTION 'order_items: the discount of an order line cannot be changed' USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

TRIGGERS = (
    ('trg_orders_coupon_frozen',
     'CREATE TRIGGER trg_orders_coupon_frozen BEFORE UPDATE OF discount_cents, coupon_id, coupon_code, coupon_snapshot ON orders FOR EACH ROW EXECUTE FUNCTION coupon_fields_frozen()'),
    ('trg_order_items_discount_frozen',
     'CREATE TRIGGER trg_order_items_discount_frozen BEFORE UPDATE OF discount_cents ON order_items FOR EACH ROW EXECUTE FUNCTION coupon_fields_frozen()'),
)


def upgrade() -> None:
    op.create_table(
        "coupons",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("percent_off", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("amount_off_cents", sa.Integer(), nullable=True),
        sa.Column("max_discount_cents", sa.Integer(), nullable=True),
        sa.Column("min_subtotal_cents", sa.Integer(), server_default="0", nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses_total", sa.Integer(), nullable=True),
        sa.Column("max_uses_per_customer", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'", name="ck_coupons_code"),
        sa.CheckConstraint("kind IN ('percentage', 'fixed', 'free_shipping')", name="ck_coupons_kind"),
        sa.CheckConstraint(
            "(kind = 'percentage' AND percent_off BETWEEN 0.01 AND 100 AND amount_off_cents IS NULL)"
            " OR (kind = 'fixed' AND amount_off_cents > 0 AND percent_off IS NULL AND max_discount_cents IS NULL)"
            " OR (kind = 'free_shipping' AND percent_off IS NULL AND amount_off_cents IS NULL AND max_discount_cents IS NULL)",
            name="ck_coupons_value",
        ),
        sa.CheckConstraint("max_discount_cents IS NULL OR max_discount_cents > 0", name="ck_coupons_cap"),
        sa.CheckConstraint("min_subtotal_cents >= 0", name="ck_coupons_minimum"),
        sa.CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_coupons_dates"),
        sa.CheckConstraint("max_uses_total IS NULL OR max_uses_total > 0", name="ck_coupons_total_limit"),
        sa.CheckConstraint("max_uses_per_customer IS NULL OR max_uses_per_customer > 0", name="ck_coupons_customer_limit"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_coupons_code"),
    )
    op.add_column("orders", sa.Column("discount_cents", sa.Integer(), server_default="0", nullable=False))
    op.add_column("orders", sa.Column("coupon_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("coupon_code", sa.String(length=32), nullable=True))
    op.add_column("orders", sa.Column("coupon_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_foreign_key("orders_coupon_id_fkey", "orders", "coupons", ["coupon_id"], ["id"], ondelete="RESTRICT")
    op.create_check_constraint("ck_orders_discount_within_subtotal", "orders", "discount_cents >= 0 AND discount_cents <= subtotal_cents")
    op.create_check_constraint("ck_orders_coupon_consistent", "orders",
                               "(coupon_id IS NULL) = (coupon_code IS NULL) AND (coupon_id IS NOT NULL OR discount_cents = 0)")
    op.create_index("ix_orders_coupon_id", "orders", ["coupon_id"], postgresql_where=sa.text("coupon_id IS NOT NULL"))
    op.add_column("order_items", sa.Column("discount_cents", sa.Integer(), server_default="0", nullable=False))
    op.create_check_constraint("ck_order_items_discount", "order_items", "discount_cents >= 0 AND discount_cents <= price_cents_snapshot * quantity")
    op.execute(FUNCTION)
    for _name, statement in TRIGGERS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_order_items_discount_frozen ON order_items")
    op.execute("DROP TRIGGER IF EXISTS trg_orders_coupon_frozen ON orders")
    op.execute("DROP FUNCTION IF EXISTS coupon_fields_frozen()")
    op.drop_constraint("ck_order_items_discount", "order_items", type_="check")
    op.drop_column("order_items", "discount_cents")
    op.drop_index("ix_orders_coupon_id", table_name="orders")
    op.drop_constraint("ck_orders_coupon_consistent", "orders", type_="check")
    op.drop_constraint("ck_orders_discount_within_subtotal", "orders", type_="check")
    op.drop_constraint("orders_coupon_id_fkey", "orders", type_="foreignkey")
    for column in ("coupon_snapshot", "coupon_code", "coupon_id", "discount_cents"):
        op.drop_column("orders", column)
    op.drop_table("coupons")
