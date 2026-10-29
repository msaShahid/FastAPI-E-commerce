"""product variants: concurrent cart indexes, constraint validation and batched back-fills

Revision ID: c7f1a9d3e526
Revises: b4d8e2a6c915
Create Date: 2026-10-29 10:30:00.000000

Resumable and safe to run again after an interruption:

  * The two partial unique indexes on cart_items are built CONCURRENTLY (no write lock). An interrupted build leaves an INVALID index,
    which is detected and dropped first. The old UNIQUE (cart_id, product_id) is dropped only after both exist: they are equivalent for
    existing data because every existing cart line has variant_id NULL.
  * Back-fills run in batches of 5,000 rows, each its own transaction, and only touch rows not yet done, so a re-run continues:
      - order_items.sku_snapshot from products.sku, marked sku_snapshot_source = 'backfill'. (Application code never changed a SKU, but
        the database did not forbid it, so the value is labelled as a back-fill rather than a recorded fact.)
      - order_items.restocked_quantity = quantity for lines of orders whose stock was already released.
    No order amount, quantity, status or timestamp is touched.
  * The NOT VALID checks and foreign keys from the previous revision are validated last.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7f1a9d3e526"
down_revision: Union[str, Sequence[str], None] = "b4d8e2a6c915"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BATCH = 5000
NEW_INDEXES = ("ux_cart_items_cart_product_simple", "ux_cart_items_cart_variant")
VALIDATIONS = (
    ("products", "ck_products_option_count"),
    ("products", "ck_products_stock_zero_with_options"),
    ("order_items", "ck_order_items_restocked_within_quantity"),
    ("order_items", "ck_order_items_sku_source"),
    ("cart_items", "fk_cart_items_variant"),
    ("order_items", "fk_order_items_variant"),
)


def _id_ranges(bind, table: str):
    low, high = bind.execute(sa.text(f"SELECT min(id), max(id) FROM {table}")).one()
    if low is None:
        return
    start = low
    while start <= high:
        yield start, start + BATCH
        start += BATCH


def upgrade() -> None:
    bind = op.get_bind()
    with op.get_context().autocommit_block():
        leftovers = bind.execute(sa.text(
            "SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid AND c.relname = ANY(:names)"
        ), {"names": list(NEW_INDEXES)}).scalars().all()
        for name in leftovers:  # an interrupted CONCURRENTLY build
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_cart_items_cart_product_simple ON cart_items (cart_id, product_id) WHERE variant_id IS NULL")
        op.execute("CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_cart_items_cart_variant ON cart_items (cart_id, variant_id) WHERE variant_id IS NOT NULL")

        for low, high in _id_ranges(bind, "order_items"):
            bind.execute(sa.text(
                "UPDATE order_items oi SET sku_snapshot = p.sku, sku_snapshot_source = 'backfill' FROM products p "
                "WHERE p.id = oi.product_id AND oi.sku_snapshot IS NULL AND oi.id >= :low AND oi.id < :high"
            ), {"low": low, "high": high})
        for low, high in _id_ranges(bind, "order_items"):
            bind.execute(sa.text(
                "UPDATE order_items oi SET restocked_quantity = oi.quantity FROM orders o "
                "WHERE o.id = oi.order_id AND o.stock_released_at IS NOT NULL AND oi.restocked_quantity = 0 "
                "AND oi.id >= :low AND oi.id < :high"
            ), {"low": low, "high": high})

    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("ALTER TABLE cart_items DROP CONSTRAINT IF EXISTS uq_cart_items_cart_product")
    for table, constraint in VALIDATIONS:
        op.execute(f"ALTER TABLE {table} VALIDATE CONSTRAINT {constraint}")


def downgrade() -> None:
    op.execute("ALTER TABLE cart_items ADD CONSTRAINT uq_cart_items_cart_product UNIQUE (cart_id, product_id)")
    for name in NEW_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
