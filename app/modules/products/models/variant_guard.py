"""Database rules for variants, SKUs, stock history and order-line integrity (gap E).

Everything a plain constraint can say is a constraint, on the models. What follows is only what a constraint CANNOT express, as functions
and triggers, installed by create_all (tests) and by migration b4d8e2a6c915 (production). A test asserts the two copies are identical.

Concurrency rules (also tested):
  * Writers of options, values and variants first take `SELECT ... FOR UPDATE` on the PRODUCT row, so changes to one product are serialised.
    This is what prevents write skew: two transactions each deactivating a DIFFERENT variant of the last two cannot both commit.
  * Lock order everywhere is product row first, then variant rows, matching checkout (products, then variants, ascending ids).
  * The 'deferred' checks run at COMMIT, after the writer's own changes are all in place.
"""

from sqlalchemy import DDL, event

from app.core.database import Base

# ------------------------------------------------------------------------------------------------------------- SKU registry
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

# ------------------------------------------------------------------------------------------------------------- stock ledger
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

# ------------------------------------------------------------------------------------------------------------- variant integrity
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

# ------------------------------------------------------------------------------------------------------------- order lines
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
    SKU_REGISTRY_FUNCTION, STOCK_LEDGER_WRITE_FUNCTION, STOCK_LEDGER_GUARD_FUNCTION, LOCK_PARENT_PRODUCT_FUNCTION,
    CHECK_OPTION_COUNT_FUNCTION, CHECK_ACTIVE_VARIANT_FUNCTION, LINE_VARIANT_RULE_FUNCTION, ORDER_ITEM_RESTOCK_FUNCTION,
    ORDER_ITEM_IMMUTABLE_FUNCTION,
)

# (trigger name, full CREATE statement). Order matters only in that every table and function exists first.
TRIGGERS = (
    ("trg_products_sku_registry",
     "CREATE TRIGGER trg_products_sku_registry AFTER INSERT OR UPDATE OF sku OR DELETE ON products FOR EACH ROW EXECUTE FUNCTION sku_registry_sync()"),
    ("trg_variants_sku_registry",
     "CREATE TRIGGER trg_variants_sku_registry AFTER INSERT OR UPDATE OF sku OR DELETE ON product_variants FOR EACH ROW EXECUTE FUNCTION sku_registry_sync()"),
    ("trg_products_stock_ledger_ins",
     "CREATE TRIGGER trg_products_stock_ledger_ins AFTER INSERT ON products FOR EACH ROW WHEN (NEW.stock <> 0) EXECUTE FUNCTION stock_ledger_write()"),
    ("trg_products_stock_ledger_upd",
     "CREATE TRIGGER trg_products_stock_ledger_upd AFTER UPDATE OF stock ON products FOR EACH ROW WHEN (OLD.stock IS DISTINCT FROM NEW.stock) EXECUTE FUNCTION stock_ledger_write()"),
    ("trg_variants_stock_ledger_ins",
     "CREATE TRIGGER trg_variants_stock_ledger_ins AFTER INSERT ON product_variants FOR EACH ROW WHEN (NEW.stock <> 0) EXECUTE FUNCTION stock_ledger_write()"),
    ("trg_variants_stock_ledger_upd",
     "CREATE TRIGGER trg_variants_stock_ledger_upd AFTER UPDATE OF stock ON product_variants FOR EACH ROW WHEN (OLD.stock IS DISTINCT FROM NEW.stock) EXECUTE FUNCTION stock_ledger_write()"),
    ("trg_stock_movements_guard",
     "CREATE TRIGGER trg_stock_movements_guard BEFORE INSERT OR UPDATE OR DELETE ON stock_movements FOR EACH ROW EXECUTE FUNCTION stock_ledger_guard()"),
    ("trg_product_options_lock",
     "CREATE TRIGGER trg_product_options_lock BEFORE INSERT OR UPDATE OR DELETE ON product_options FOR EACH ROW EXECUTE FUNCTION lock_parent_product()"),
    ("trg_option_values_lock",
     "CREATE TRIGGER trg_option_values_lock BEFORE INSERT OR UPDATE OR DELETE ON product_option_values FOR EACH ROW EXECUTE FUNCTION lock_parent_product()"),
    ("trg_variants_lock",
     "CREATE TRIGGER trg_variants_lock BEFORE INSERT OR UPDATE OR DELETE ON product_variants FOR EACH ROW EXECUTE FUNCTION lock_parent_product()"),
    ("trg_products_option_count_ins",
     "CREATE CONSTRAINT TRIGGER trg_products_option_count_ins AFTER INSERT ON products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.option_count <> 0) EXECUTE FUNCTION check_option_count()"),
    ("trg_products_option_count_upd",
     "CREATE CONSTRAINT TRIGGER trg_products_option_count_upd AFTER UPDATE OF option_count ON products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (OLD.option_count IS DISTINCT FROM NEW.option_count) EXECUTE FUNCTION check_option_count()"),
    ("trg_product_options_count",
     "CREATE CONSTRAINT TRIGGER trg_product_options_count AFTER INSERT OR UPDATE OR DELETE ON product_options DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_option_count()"),
    ("trg_products_active_variant_rule",
     "CREATE CONSTRAINT TRIGGER trg_products_active_variant_rule AFTER INSERT OR UPDATE OF status, option_count ON products DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.option_count > 0) EXECUTE FUNCTION check_active_variant_rule()"),
    ("trg_variants_active_variant_rule",
     "CREATE CONSTRAINT TRIGGER trg_variants_active_variant_rule AFTER INSERT OR UPDATE OF status OR DELETE ON product_variants DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_active_variant_rule()"),
    ("trg_cart_items_variant_rule",
     "CREATE TRIGGER trg_cart_items_variant_rule BEFORE INSERT OR UPDATE OF product_id, variant_id ON cart_items FOR EACH ROW EXECUTE FUNCTION enforce_line_variant_rule()"),
    ("trg_order_items_variant_rule",
     "CREATE TRIGGER trg_order_items_variant_rule BEFORE INSERT ON order_items FOR EACH ROW EXECUTE FUNCTION enforce_line_variant_rule()"),
    ("trg_order_items_immutable",
     "CREATE TRIGGER trg_order_items_immutable BEFORE UPDATE ON order_items FOR EACH ROW EXECUTE FUNCTION order_items_immutable()"),
    ("trg_order_item_restocks_apply",
     "CREATE TRIGGER trg_order_item_restocks_apply AFTER INSERT ON order_item_restocks FOR EACH ROW EXECUTE FUNCTION order_item_restock_apply()"),
)

# One statement per entry (the driver executes a single command at a time).
GUARD_STATEMENTS = (*FUNCTIONS, *(sql for _name, sql in TRIGGERS))

for _sql in GUARD_STATEMENTS:
    # DDL() does %-formatting, and the function bodies contain literal % (RAISE placeholders): double them.
    event.listen(Base.metadata, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
