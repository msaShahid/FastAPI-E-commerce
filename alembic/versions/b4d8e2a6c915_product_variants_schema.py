"""product variants, SKU registry, stock ledger and per-line restock events (schema and database rules)

Revision ID: b4d8e2a6c915
Revises: a3e7c1b9d542
Create Date: 2026-10-29 10:00:00.000000

Additive. Existing products, carts, orders, payments and amounts are not changed. Every new column is nullable or defaulted, so existing rows
and the running application keep working while this is applied:

  * products.option_count (default 0 = a simple product); cart_items, order_items and orders get nullable variant columns.
  * New tables: product_options, product_option_values, product_variants, sku_registry, stock_movements, order_item_restocks.
  * Pre-flight (nothing is changed if it fails): PostgreSQL 15+, and no SKUs that differ only by case or surrounding spaces.
  * The SKU registry is back-filled from products; an OPENING row per product with stock is written to the stock ledger so that the
    ledger's sum equals the stock from day one.
  * New CHECKs and foreign keys are added NOT VALID here and validated in the next revision (c7f1a9d3e526), which also builds the cart
    indexes CONCURRENTLY and back-fills order items in batches.

The guard function and trigger text below is identical to app/modules/products/models/variant_guard.py (a test asserts it).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4d8e2a6c915"
down_revision: Union[str, Sequence[str], None] = "a3e7c1b9d542"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SKU_REGISTRY_FUNCTION = r'''CREATE OR REPLACE FUNCTION sku_registry_sync() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM sku_registry WHERE sku_norm = upper(btrim(OLD.sku));
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF NEW.sku IS DISTINCT FROM OLD.sku THEN
            RAISE EXCEPTION 'a SKU cannot be changed once created'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'products' THEN
        INSERT INTO sku_registry (sku_norm, product_id) VALUES (upper(btrim(NEW.sku)), NEW.id);
    ELSE
        INSERT INTO sku_registry (sku_norm, variant_id) VALUES (upper(btrim(NEW.sku)), NEW.id);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

STOCK_LEDGER_WRITE_FUNCTION = r'''CREATE OR REPLACE FUNCTION stock_ledger_write() RETURNS trigger AS $$
DECLARE
    movement_kind text;
    before_qty integer;
    pid integer;
    vid integer;
BEGIN
    movement_kind := nullif(current_setting('novacart.stock_kind', true), '');
    IF TG_OP = 'INSERT' THEN
        before_qty := 0;
        movement_kind := coalesce(movement_kind, 'initial');
    ELSE
        before_qty := OLD.stock;
        movement_kind := coalesce(movement_kind, 'unspecified');
    END IF;
    IF TG_TABLE_NAME = 'products' THEN
        pid := NEW.id;
        vid := NULL;
    ELSE
        pid := NEW.product_id;
        vid := NEW.id;
    END IF;
    INSERT INTO stock_movements (kind, product_id, variant_id, delta, before_qty, after_qty, actor_user_id, reason, order_id)
    VALUES (
        movement_kind, pid, vid, NEW.stock - before_qty, before_qty, NEW.stock,
        nullif(current_setting('novacart.stock_actor', true), '')::uuid,
        nullif(current_setting('novacart.stock_reason', true), ''),
        nullif(current_setting('novacart.stock_order', true), '')::uuid
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;'''

STOCK_LEDGER_GUARD_FUNCTION = r'''CREATE OR REPLACE FUNCTION stock_ledger_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        RAISE EXCEPTION 'the stock ledger is append-only'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'stock ledger rows are written only by the stock triggers'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

LOCK_PARENT_PRODUCT_FUNCTION = r'''CREATE OR REPLACE FUNCTION lock_parent_product() RETURNS trigger AS $$
DECLARE
    pid integer;
BEGIN
    IF TG_OP = 'DELETE' THEN
        pid := OLD.product_id;
    ELSE
        pid := NEW.product_id;
    END IF;
    PERFORM 1 FROM products WHERE id = pid FOR UPDATE;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

CHECK_OPTION_COUNT_FUNCTION = r'''CREATE OR REPLACE FUNCTION check_option_count() RETURNS trigger AS $$
DECLARE
    pid integer;
    declared smallint;
    actual integer;
    highest integer;
BEGIN
    IF TG_TABLE_NAME = 'products' THEN
        pid := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        pid := OLD.product_id;
    ELSE
        pid := NEW.product_id;
    END IF;
    SELECT option_count INTO declared FROM products WHERE id = pid;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    SELECT count(*), coalesce(max(position), 0) INTO actual, highest FROM product_options WHERE product_id = pid;
    IF actual <> declared OR highest <> actual THEN
        RAISE EXCEPTION 'product % declares % option(s) but has % (positions must be 1..n)', pid, declared, actual
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;'''

CHECK_ACTIVE_VARIANT_FUNCTION = r'''CREATE OR REPLACE FUNCTION check_active_variant_rule() RETURNS trigger AS $$
DECLARE
    pid integer;
    product_status text;
    options smallint;
BEGIN
    IF TG_TABLE_NAME = 'products' THEN
        pid := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        pid := OLD.product_id;
    ELSE
        pid := NEW.product_id;
    END IF;
    SELECT status::text, option_count INTO product_status, options FROM products WHERE id = pid;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF options > 0 AND product_status = 'active'
       AND NOT EXISTS (SELECT 1 FROM product_variants WHERE product_id = pid AND status = 'active') THEN
        RAISE EXCEPTION 'an active product with options needs at least one active variant (product %)', pid
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;'''

LINE_VARIANT_RULE_FUNCTION = r'''CREATE OR REPLACE FUNCTION enforce_line_variant_rule() RETURNS trigger AS $$
DECLARE
    options smallint;
BEGIN
    SELECT option_count INTO options FROM products WHERE id = NEW.product_id FOR KEY SHARE;
    IF options IS NULL THEN
        RETURN NEW;
    END IF;
    IF options > 0 AND NEW.variant_id IS NULL THEN
        RAISE EXCEPTION 'product % has options: a variant must be chosen', NEW.product_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF options = 0 AND NEW.variant_id IS NOT NULL THEN
        RAISE EXCEPTION 'product % has no options: it has no variants', NEW.product_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

ORDER_ITEM_RESTOCK_FUNCTION = r'''CREATE OR REPLACE FUNCTION order_item_restock_apply() RETURNS trigger AS $$
BEGIN
    UPDATE order_items SET restocked_quantity = restocked_quantity + NEW.quantity WHERE id = NEW.order_item_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

ORDER_ITEM_IMMUTABLE_FUNCTION = r'''CREATE OR REPLACE FUNCTION order_items_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.order_id IS DISTINCT FROM OLD.order_id
       OR NEW.product_id IS DISTINCT FROM OLD.product_id
       OR NEW.variant_id IS DISTINCT FROM OLD.variant_id
       OR NEW.product_name_snapshot IS DISTINCT FROM OLD.product_name_snapshot
       OR NEW.price_cents_snapshot IS DISTINCT FROM OLD.price_cents_snapshot
       OR NEW.quantity IS DISTINCT FROM OLD.quantity
       OR NEW.variant_snapshot IS DISTINCT FROM OLD.variant_snapshot
       OR (OLD.sku_snapshot IS NOT NULL AND NEW.sku_snapshot IS DISTINCT FROM OLD.sku_snapshot) THEN
        RAISE EXCEPTION 'order item details are a historical record and cannot be changed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

FUNCTIONS = (
    SKU_REGISTRY_FUNCTION, STOCK_LEDGER_WRITE_FUNCTION, STOCK_LEDGER_GUARD_FUNCTION, LOCK_PARENT_PRODUCT_FUNCTION, CHECK_OPTION_COUNT_FUNCTION, CHECK_ACTIVE_VARIANT_FUNCTION, LINE_VARIANT_RULE_FUNCTION, ORDER_ITEM_RESTOCK_FUNCTION, ORDER_ITEM_IMMUTABLE_FUNCTION,
)

TRIGGERS = (
    ('trg_products_sku_registry',
     'CREATE TRIGGER trg_products_sku_registry AFTER INSERT OR UPDATE OF sku OR DELETE ON products FOR EACH ROW EXECUTE FUNCTION sku_registry_sync()'),
    ('trg_variants_sku_registry',
     'CREATE TRIGGER trg_variants_sku_registry AFTER INSERT OR UPDATE OF sku OR DELETE ON product_variants FOR EACH ROW EXECUTE FUNCTION sku_registry_sync()'),
    ('trg_products_stock_ledger_ins',
     'CREATE TRIGGER trg_products_stock_ledger_ins AFTER INSERT ON products FOR EACH ROW WHEN (NEW.stock <> 0) EXECUTE FUNCTION stock_ledger_write()'),
    ('trg_products_stock_ledger_upd',
     'CREATE TRIGGER trg_products_stock_ledger_upd AFTER UPDATE OF stock ON products FOR EACH ROW WHEN (OLD.stock IS DISTINCT FROM NEW.stock) EXECUTE FUNCTION stock_ledger_write()'),
    ('trg_variants_stock_ledger_ins',
     'CREATE TRIGGER trg_variants_stock_ledger_ins AFTER INSERT ON product_variants FOR EACH ROW WHEN (NEW.stock <> 0) EXECUTE FUNCTION stock_ledger_write()'),
    ('trg_variants_stock_ledger_upd',
     'CREATE TRIGGER trg_variants_stock_ledger_upd AFTER UPDATE OF stock ON product_variants FOR EACH ROW WHEN (OLD.stock IS DISTINCT FROM NEW.stock) EXECUTE FUNCTION stock_ledger_write()'),
    ('trg_stock_movements_guard',
     'CREATE TRIGGER trg_stock_movements_guard BEFORE INSERT OR UPDATE OR DELETE ON stock_movements FOR EACH ROW EXECUTE FUNCTION stock_ledger_guard()'),
    ('trg_product_options_lock',
     'CREATE TRIGGER trg_product_options_lock BEFORE INSERT OR UPDATE OR DELETE ON product_options FOR EACH ROW EXECUTE FUNCTION lock_parent_product()'),
    ('trg_option_values_lock',
     'CREATE TRIGGER trg_option_values_lock BEFORE INSERT OR UPDATE OR DELETE ON product_option_values FOR EACH ROW EXECUTE FUNCTION lock_parent_product()'),
    ('trg_variants_lock',
     'CREATE TRIGGER trg_variants_lock BEFORE INSERT OR UPDATE OR DELETE ON product_variants FOR EACH ROW EXECUTE FUNCTION lock_parent_product()'),
    ('trg_products_option_count_ins',
     'CREATE CONSTRAINT TRIGGER trg_products_option_count_ins AFTER INSERT ON products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.option_count <> 0) EXECUTE FUNCTION check_option_count()'),
    ('trg_products_option_count_upd',
     'CREATE CONSTRAINT TRIGGER trg_products_option_count_upd AFTER UPDATE OF option_count ON products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.option_count IS DISTINCT FROM NEW.option_count) EXECUTE FUNCTION check_option_count()'),
    ('trg_product_options_count',
     'CREATE CONSTRAINT TRIGGER trg_product_options_count AFTER INSERT OR UPDATE OR DELETE ON product_options DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_option_count()'),
    ('trg_products_active_variant_rule',
     'CREATE CONSTRAINT TRIGGER trg_products_active_variant_rule AFTER INSERT OR UPDATE OF status, option_count ON products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.option_count > 0) EXECUTE FUNCTION check_active_variant_rule()'),
    ('trg_variants_active_variant_rule',
     'CREATE CONSTRAINT TRIGGER trg_variants_active_variant_rule AFTER INSERT OR UPDATE OF status OR DELETE ON product_variants DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_active_variant_rule()'),
    ('trg_cart_items_variant_rule',
     'CREATE TRIGGER trg_cart_items_variant_rule BEFORE INSERT OR UPDATE OF product_id, variant_id ON cart_items FOR EACH ROW EXECUTE FUNCTION enforce_line_variant_rule()'),
    ('trg_order_items_variant_rule',
     'CREATE TRIGGER trg_order_items_variant_rule BEFORE INSERT ON order_items FOR EACH ROW EXECUTE FUNCTION enforce_line_variant_rule()'),
    ('trg_order_items_immutable',
     'CREATE TRIGGER trg_order_items_immutable BEFORE UPDATE ON order_items FOR EACH ROW EXECUTE FUNCTION order_items_immutable()'),
    ('trg_order_item_restocks_apply',
     'CREATE TRIGGER trg_order_item_restocks_apply AFTER INSERT ON order_item_restocks FOR EACH ROW EXECUTE FUNCTION order_item_restock_apply()'),
)


def _preflight(bind) -> None:
    version = int(bind.exec_driver_sql("SHOW server_version_num").scalar())
    if version < 150000:
        raise RuntimeError(
            f"Product variants need PostgreSQL 15 or newer (this server reports {version}). Nothing was changed."
        )
    duplicates = bind.exec_driver_sql(
        "SELECT upper(btrim(sku)), count(*), string_agg(id::text, ', ' ORDER BY id) FROM products "
        "GROUP BY 1 HAVING count(*) > 1 ORDER BY 1 LIMIT 20"
    ).all()
    if duplicates:
        listing = "; ".join(f"{sku!r} on product ids {ids}" for sku, _n, ids in duplicates)
        raise RuntimeError(
            "These product SKUs differ only by letter case or surrounding spaces and would collide in the case-insensitive SKU "
            f"registry: {listing}. Nothing was changed. Rename the duplicates (UPDATE products SET sku = ... for ONE of each pair, "
            "after making sure nothing external depends on it), then run the migration again."
        )
    padded = bind.exec_driver_sql("SELECT id, sku FROM products WHERE sku <> btrim(sku) OR btrim(sku) = '' ORDER BY id LIMIT 20").all()
    if padded:
        raise RuntimeError(
            f"These products have a blank SKU or one with leading/trailing spaces: {padded}. Nothing was changed. Fix them, then run again."
        )


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)
    op.execute("SET LOCAL lock_timeout = '3s'")  # fail fast instead of queueing behind long transactions

    # ---- products: the option count and its checks (validated in the next revision)
    op.add_column("products", sa.Column("option_count", sa.SmallInteger(), server_default="0", nullable=False))
    op.execute("ALTER TABLE products ADD CONSTRAINT ck_products_option_count CHECK (option_count BETWEEN 0 AND 3) NOT VALID")
    op.execute("ALTER TABLE products ADD CONSTRAINT ck_products_stock_zero_with_options CHECK (option_count = 0 OR stock = 0) NOT VALID")
    op.create_unique_constraint("uq_products_id_option_count", "products", ["id", "option_count"])

    # ---- options, values, variants
    op.create_table(
        "product_options",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("position BETWEEN 1 AND 3", name="ck_product_options_position"),
        sa.CheckConstraint("btrim(name) <> '' AND name = btrim(name)", name="ck_product_options_name"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "position", name="uq_product_options_product_position"),
        sa.UniqueConstraint("id", "product_id", "position", name="uq_product_options_id_product_position"),
    )
    op.create_index("ux_product_options_product_lname", "product_options", ["product_id", sa.text("lower(name)")], unique=True)
    op.create_table(
        "product_option_values",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("option_id", sa.Integer(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("option_position", sa.SmallInteger(), nullable=False),
        sa.Column("value", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(value) <> '' AND value = btrim(value)", name="ck_option_values_value"),
        sa.ForeignKeyConstraint(
            ["option_id", "product_id", "option_position"],
            ["product_options.id", "product_options.product_id", "product_options.position"],
            name="fk_option_values_option", onupdate="RESTRICT", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "product_id", "option_position", name="uq_option_values_id_product_position"),
    )
    op.create_index("ux_option_values_option_lvalue", "product_option_values", ["option_id", sa.text("lower(value)")], unique=True)
    value_columns = ["product_option_values.id", "product_option_values.product_id", "product_option_values.option_position"]
    op.create_table(
        "product_variants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("option_count", sa.SmallInteger(), nullable=False),
        sa.Column("value1_id", sa.Integer(), nullable=True),
        sa.Column("value2_id", sa.Integer(), nullable=True),
        sa.Column("value3_id", sa.Integer(), nullable=True),
        sa.Column("slot1", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("slot2", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("slot3", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("price_override_cents", sa.Integer(), nullable=True),
        sa.Column("stock", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=10), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("slot1 = 1 AND slot2 = 2 AND slot3 = 3", name="ck_variants_slots"),
        sa.CheckConstraint("option_count BETWEEN 1 AND 3", name="ck_variants_option_count"),
        sa.CheckConstraint(
            "(value1_id IS NOT NULL) = (option_count >= 1) AND (value2_id IS NOT NULL) = (option_count >= 2) "
            "AND (value3_id IS NOT NULL) = (option_count >= 3)",
            name="ck_variants_complete_combination",
        ),
        sa.CheckConstraint("price_override_cents IS NULL OR price_override_cents > 0", name="ck_variants_price_override"),
        sa.CheckConstraint("stock >= 0", name="ck_variants_stock_non_negative"),
        sa.CheckConstraint("status IN ('active', 'inactive')", name="ck_variants_status"),
        sa.CheckConstraint("btrim(sku) <> '' AND sku = btrim(sku)", name="ck_variants_sku"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_variants_product", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["product_id", "option_count"], ["products.id", "products.option_count"],
            name="fk_variants_product_option_count", onupdate="RESTRICT", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["value1_id", "product_id", "slot1"], value_columns, name="fk_variants_value1", onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["value2_id", "product_id", "slot2"], value_columns, name="fk_variants_value2", onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["value3_id", "product_id", "slot3"], value_columns, name="fk_variants_value3", onupdate="RESTRICT", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "value1_id", "value2_id", "value3_id", name="uq_variants_combination", postgresql_nulls_not_distinct=True),
        sa.UniqueConstraint("id", "product_id", name="uq_variants_id_product"),
    )
    op.create_index("ix_product_variants_product_status", "product_variants", ["product_id", "status"])

    # ---- SKU registry, stock ledger, per-line restock events
    op.create_table(
        "sku_registry",
        sa.Column("sku_norm", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=True),
        sa.Column("variant_id", sa.Integer(), nullable=True),
        sa.CheckConstraint("(product_id IS NULL) <> (variant_id IS NULL)", name="ck_sku_registry_one_owner"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("sku_norm"),
    )
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("variant_id", sa.Integer(), nullable=True),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("before_qty", sa.Integer(), nullable=False),
        sa.Column("after_qty", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "kind IN ('opening', 'initial', 'sale', 'restock', 'admin_set', 'admin_delta', 'conversion', 'unspecified')",
            name="ck_stock_movements_kind",
        ),
        sa.CheckConstraint("delta <> 0", name="ck_stock_movements_delta"),
        sa.CheckConstraint("after_qty = before_qty + delta AND after_qty >= 0 AND before_qty >= 0", name="ck_stock_movements_balance"),
        sa.CheckConstraint(
            "kind NOT IN ('admin_set', 'admin_delta') OR (actor_user_id IS NOT NULL AND length(btrim(reason)) >= 3)",
            name="ck_stock_movements_admin_reason",
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["variant_id"], ["product_variants.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="NO ACTION"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id", "id"])
    op.create_index("ix_stock_movements_variant_id", "stock_movements", ["variant_id", "id"], postgresql_where=sa.text("variant_id IS NOT NULL"))

    # ---- nullable columns on existing tables (metadata-only: no rewrite, no data change)
    op.add_column("cart_items", sa.Column("variant_id", sa.Integer(), nullable=True))
    op.execute(
        "ALTER TABLE cart_items ADD CONSTRAINT fk_cart_items_variant FOREIGN KEY (variant_id, product_id) "
        "REFERENCES product_variants (id, product_id) ON DELETE RESTRICT NOT VALID"
    )
    op.add_column("order_items", sa.Column("variant_id", sa.Integer(), nullable=True))
    op.add_column("order_items", sa.Column("sku_snapshot", sa.String(length=64), nullable=True))
    op.add_column("order_items", sa.Column("sku_snapshot_source", sa.String(length=10), nullable=True))
    op.add_column("order_items", sa.Column("variant_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("order_items", sa.Column("restocked_quantity", sa.Integer(), server_default="0", nullable=False))
    op.execute("ALTER TABLE order_items ADD CONSTRAINT ck_order_items_restocked_within_quantity CHECK (restocked_quantity >= 0 AND restocked_quantity <= quantity) NOT VALID")
    op.execute("ALTER TABLE order_items ADD CONSTRAINT ck_order_items_sku_source CHECK (sku_snapshot_source IS NULL OR sku_snapshot_source IN ('order', 'backfill')) NOT VALID")
    op.execute(
        "ALTER TABLE order_items ADD CONSTRAINT fk_order_items_variant FOREIGN KEY (variant_id, product_id) "
        "REFERENCES product_variants (id, product_id) ON DELETE RESTRICT NOT VALID"
    )
    op.add_column("orders", sa.Column("cart_digest", sa.String(length=64), nullable=True))
    op.create_table(
        "order_item_restocks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_item_restocks_quantity"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_item_id", "event_key", name="uq_order_item_restocks_event"),
    )

    # ---- database rules
    for statement in FUNCTIONS:
        op.execute(statement)
    for _name, statement in TRIGGERS:
        op.execute(statement)

    # ---- back-fills small enough for this transaction
    op.execute("INSERT INTO sku_registry (sku_norm, product_id) SELECT upper(btrim(sku)), id FROM products")
    # The ledger accepts rows only from its own triggers: this one-off opening balance is written with that guard switched off.
    op.execute("ALTER TABLE stock_movements DISABLE TRIGGER trg_stock_movements_guard")
    op.execute(
        "INSERT INTO stock_movements (kind, product_id, delta, before_qty, after_qty, reason) "
        "SELECT 'opening', id, stock, 0, stock, 'balance when the stock ledger was introduced' FROM products WHERE stock > 0"
    )
    op.execute("ALTER TABLE stock_movements ENABLE TRIGGER trg_stock_movements_guard")


TRIGGER_TABLES = {
    "trg_products_sku_registry": "products", "trg_variants_sku_registry": "product_variants",
    "trg_products_stock_ledger_ins": "products", "trg_products_stock_ledger_upd": "products",
    "trg_variants_stock_ledger_ins": "product_variants", "trg_variants_stock_ledger_upd": "product_variants",
    "trg_stock_movements_guard": "stock_movements", "trg_product_options_lock": "product_options",
    "trg_option_values_lock": "product_option_values", "trg_variants_lock": "product_variants",
    "trg_products_option_count_ins": "products", "trg_products_option_count_upd": "products",
    "trg_product_options_count": "product_options", "trg_products_active_variant_rule": "products",
    "trg_variants_active_variant_rule": "product_variants", "trg_cart_items_variant_rule": "cart_items",
    "trg_order_items_variant_rule": "order_items", "trg_order_items_immutable": "order_items",
    "trg_order_item_restocks_apply": "order_item_restocks",
}


def downgrade() -> None:
    for name, _sql in reversed(TRIGGERS):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON {TRIGGER_TABLES[name]}")
    for function in (
        "order_items_immutable", "order_item_restock_apply", "enforce_line_variant_rule", "check_active_variant_rule",
        "check_option_count", "lock_parent_product", "stock_ledger_guard", "stock_ledger_write", "sku_registry_sync",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_table("order_item_restocks")
    op.drop_column("orders", "cart_digest")
    op.execute("ALTER TABLE order_items DROP CONSTRAINT IF EXISTS fk_order_items_variant")
    op.execute("ALTER TABLE order_items DROP CONSTRAINT IF EXISTS ck_order_items_sku_source")
    op.execute("ALTER TABLE order_items DROP CONSTRAINT IF EXISTS ck_order_items_restocked_within_quantity")
    for column in ("restocked_quantity", "variant_snapshot", "sku_snapshot_source", "sku_snapshot", "variant_id"):
        op.drop_column("order_items", column)
    op.execute("ALTER TABLE cart_items DROP CONSTRAINT IF EXISTS fk_cart_items_variant")
    op.drop_column("cart_items", "variant_id")
    op.drop_table("stock_movements")
    op.drop_table("sku_registry")
    op.drop_table("product_variants")
    op.drop_table("product_option_values")
    op.drop_table("product_options")
    op.drop_constraint("uq_products_id_option_count", "products", type_="unique")
    op.execute("ALTER TABLE products DROP CONSTRAINT IF EXISTS ck_products_stock_zero_with_options")
    op.execute("ALTER TABLE products DROP CONSTRAINT IF EXISTS ck_products_option_count")
    op.drop_column("products", "option_count")
