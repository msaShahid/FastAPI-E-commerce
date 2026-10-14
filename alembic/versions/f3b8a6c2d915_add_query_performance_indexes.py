"""add query performance indexes

Revision ID: f3b8a6c2d915
Revises: d4a91c7e5b20
Create Date: 2026-10-13 19:00:00.000000

Found by running EXPLAIN ANALYZE on every hot query against ~1.7M rows.
Each index below turns a sequential scan (plus a sort) into an index scan.

    ix_products_status_created_at         storefront listing, newest first
    ix_products_created_at                admin product list (all statuses), newest first
    ix_products_category_status_created   same, filtered by category
                                          (replaces ix_products_category_id)
    ix_products_name_trgm                 product search: name ILIKE '%term%'
    ix_orders_user_id_created_at          "my orders" (replaces ix_orders_user_id)
    ix_orders_created_at                  admin all-orders, newest first
    ix_orders_status_created_at           admin status filter + stale-order reaper
    ix_order_items_product_id             FK to products (checked on every product delete)
    ix_cart_items_product_id              FK to products (same)
    ix_audit_logs_created_at              audit list, newest first
    ix_users_created_at                   admin user list, newest first

These are ordinary CREATE INDEX statements, which block writes to the table
while the index builds. That is instant on small tables. On a large, busy
production table, build the index yourself first with
CREATE INDEX CONCURRENTLY <same name> ON ...; this migration then skips it
(every create/drop here is IF [NOT] EXISTS), so it is safe either way.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f3b8a6c2d915'
down_revision: Union[str, Sequence[str], None] = 'd4a91c7e5b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Needed by the gin_trgm_ops operator class below.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # products
    op.create_index('ix_products_status_created_at', 'products', ['status', 'created_at'], if_not_exists=True)
    op.create_index('ix_products_created_at', 'products', ['created_at'], if_not_exists=True)
    op.create_index(
        'ix_products_category_status_created',
        'products',
        ['category_id', 'status', 'created_at'],
        if_not_exists=True,
    )
    op.create_index(
        'ix_products_name_trgm',
        'products',
        ['name'],
        postgresql_using='gin',
        postgresql_ops={'name': 'gin_trgm_ops'},
        if_not_exists=True,
    )
    op.drop_index('ix_products_category_id', table_name='products', if_exists=True)

    # orders
    op.create_index('ix_orders_user_id_created_at', 'orders', ['user_id', 'created_at'], if_not_exists=True)
    op.create_index('ix_orders_created_at', 'orders', ['created_at'], if_not_exists=True)
    op.create_index('ix_orders_status_created_at', 'orders', ['status', 'created_at'], if_not_exists=True)
    op.drop_index('ix_orders_user_id', table_name='orders', if_exists=True)

    # foreign keys to products
    op.create_index('ix_order_items_product_id', 'order_items', ['product_id'], if_not_exists=True)
    op.create_index('ix_cart_items_product_id', 'cart_items', ['product_id'], if_not_exists=True)

    # append-only / admin lists
    op.create_index('ix_audit_logs_created_at', 'audit_logs', ['created_at'], if_not_exists=True)
    op.create_index('ix_users_created_at', 'users', ['created_at'], if_not_exists=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_users_created_at', table_name='users', if_exists=True)
    op.drop_index('ix_audit_logs_created_at', table_name='audit_logs', if_exists=True)

    op.drop_index('ix_cart_items_product_id', table_name='cart_items', if_exists=True)
    op.drop_index('ix_order_items_product_id', table_name='order_items', if_exists=True)

    op.create_index('ix_orders_user_id', 'orders', ['user_id'], if_not_exists=True)
    op.drop_index('ix_orders_status_created_at', table_name='orders', if_exists=True)
    op.drop_index('ix_orders_created_at', table_name='orders', if_exists=True)
    op.drop_index('ix_orders_user_id_created_at', table_name='orders', if_exists=True)

    op.create_index('ix_products_category_id', 'products', ['category_id'], if_not_exists=True)
    op.drop_index('ix_products_name_trgm', table_name='products', if_exists=True)
    op.drop_index('ix_products_category_status_created', table_name='products', if_exists=True)
    op.drop_index('ix_products_status_created_at', table_name='products', if_exists=True)
    op.drop_index('ix_products_created_at', table_name='products', if_exists=True)
    # pg_trgm is left installed: other objects may use it, and it is harmless.
