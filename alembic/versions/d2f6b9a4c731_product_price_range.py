"""product price range: lowest and highest effective price, maintained by the database

Revision ID: d2f6b9a4c731
Revises: c7f1a9d3e526
Create Date: 2026-10-30 10:00:00.000000

Adds products.price_from_cents / price_to_cents (the lowest and highest price a product is sold at: its own price for a simple
product, the range over its ACTIVE variants otherwise) with the triggers that keep them current, and two indexes for price
filtering and sorting.

  * Columns are added NOT NULL DEFAULT 0 (metadata-only, no rewrite); the CHECK is added NOT VALID and validated last.
  * Indexes are built CONCURRENTLY (an interrupted build leaves an INVALID index, which a re-run drops and rebuilds).
  * The back-fill sets from = to = price for existing products in batches of 5,000, each its own transaction, only for rows still
    at 0, so a re-run continues where it stopped. It changes no price, stock, status or timestamp.

The function and trigger text below is identical to app/modules/products/models/price_guard.py (a test asserts it).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2f6b9a4c731"
down_revision: Union[str, Sequence[str], None] = "c7f1a9d3e526"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH = 5000
NEW_INDEXES = ("ix_products_price_from_cents", "ix_products_price_to_cents")

PRICE_RANGE_FUNCTION = r'''CREATE OR REPLACE FUNCTION product_price_range(pid integer, base integer, options smallint, OUT price_from integer, OUT price_to integer) AS $$
BEGIN
    IF options = 0 THEN
        price_from := base;
        price_to := base;
        RETURN;
    END IF;
    SELECT min(coalesce(price_override_cents, base)), max(coalesce(price_override_cents, base))
      INTO price_from, price_to
      FROM product_variants WHERE product_id = pid AND status = 'active';
    IF price_from IS NULL THEN
        price_from := base;
        price_to := base;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE;'''

PRODUCT_PRICE_SUMMARY_FUNCTION = r'''CREATE OR REPLACE FUNCTION products_price_summary() RETURNS trigger AS $$
BEGIN
    SELECT r.price_from, r.price_to INTO NEW.price_from_cents, NEW.price_to_cents
      FROM product_price_range(NEW.id, NEW.price_cents, NEW.option_count) AS r;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

VARIANT_PRICE_SUMMARY_FUNCTION = r'''CREATE OR REPLACE FUNCTION variants_price_summary() RETURNS trigger AS $$
DECLARE
    pid integer;
    base integer;
    options smallint;
    new_from integer;
    new_to integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        pid := OLD.product_id;
    ELSE
        pid := NEW.product_id;
    END IF;
    SELECT price_cents, option_count INTO base, options FROM products WHERE id = pid;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT r.price_from, r.price_to INTO new_from, new_to FROM product_price_range(pid, base, options) AS r;
    UPDATE products SET price_from_cents = new_from, price_to_cents = new_to
     WHERE id = pid AND (price_from_cents, price_to_cents) IS DISTINCT FROM (new_from, new_to);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;'''

FUNCTIONS = (PRICE_RANGE_FUNCTION, PRODUCT_PRICE_SUMMARY_FUNCTION, VARIANT_PRICE_SUMMARY_FUNCTION)

TRIGGERS = (
    ('trg_products_price_summary',
     'CREATE TRIGGER trg_products_price_summary BEFORE INSERT OR UPDATE OF price_cents, option_count ON products FOR EACH ROW EXECUTE FUNCTION products_price_summary()'),
    ('trg_variants_price_summary',
     'CREATE TRIGGER trg_variants_price_summary AFTER INSERT OR UPDATE OF price_override_cents, status OR DELETE ON product_variants FOR EACH ROW EXECUTE FUNCTION variants_price_summary()'),
)

def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.add_column("products", sa.Column("price_from_cents", sa.Integer(), server_default="0", nullable=False))
    op.add_column("products", sa.Column("price_to_cents", sa.Integer(), server_default="0", nullable=False))
    op.execute("ALTER TABLE products ADD CONSTRAINT ck_products_price_range CHECK (price_from_cents <= price_to_cents) NOT VALID")
    for statement in FUNCTIONS:
        op.execute(statement)
    for _name, statement in TRIGGERS:
        op.execute(statement)

    with op.get_context().autocommit_block():
        leftovers = bind.execute(sa.text(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid AND c.relname = ANY(:names)"
        ), {"names": list(NEW_INDEXES)}).scalars().all()
        for name in leftovers:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_products_price_from_cents ON products (price_from_cents)")
        op.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_products_price_to_cents ON products (price_to_cents)")
        low, high = bind.execute(sa.text("SELECT min(id), max(id) FROM products")).one()
        if low is not None:
            start = low
            while start <= high:
                bind.execute(sa.text(
                    "UPDATE products SET price_from_cents = price_cents, price_to_cents = price_cents "
                    "WHERE id >= :low AND id < :high AND price_from_cents = 0"
                ), {"low": start, "high": start + BATCH})
                start += BATCH

    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE products VALIDATE CONSTRAINT ck_products_price_range")


def downgrade() -> None:
    for name, table in (("trg_variants_price_summary", "product_variants"), ("trg_products_price_summary", "products")):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for function in ("variants_price_summary", "products_price_summary"):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.execute("DROP FUNCTION IF EXISTS product_price_range(integer, integer, smallint)")
    for name in NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    op.execute("ALTER TABLE products DROP CONSTRAINT IF EXISTS ck_products_price_range")
    op.drop_column("products", "price_to_cents")
    op.drop_column("products", "price_from_cents")
