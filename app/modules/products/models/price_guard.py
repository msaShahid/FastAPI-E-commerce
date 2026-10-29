"""The product price range (gap E2), maintained INSIDE the database so it cannot drift.

`products.price_from_cents` / `price_to_cents` are the lowest and highest EFFECTIVE price a product is sold at:
  * a simple product: its own price, from = to;
  * a product with options: the lowest and highest of coalesce(variant override, product price) over its ACTIVE variants, whether or
    not they are in stock (so the catalog does not jump around as stock moves). With no active variant it falls back to the base price.
They exist for SQL filtering and sorting; API responses compute the same numbers from the variants they already load, and a test
asserts the two agree.

Concurrency: variant writers already hold the parent product row (lock_parent_product, variant_guard.py), so these updates are
serialised with product price edits."""

from sqlalchemy import DDL, event

from app.core.database import Base

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
    ("trg_products_price_summary",
     "CREATE TRIGGER trg_products_price_summary BEFORE INSERT OR UPDATE OF price_cents, option_count ON products FOR EACH ROW EXECUTE FUNCTION products_price_summary()"),
    ("trg_variants_price_summary",
     "CREATE TRIGGER trg_variants_price_summary AFTER INSERT OR UPDATE OF price_override_cents, status OR DELETE ON product_variants FOR EACH ROW EXECUTE FUNCTION variants_price_summary()"),
)

GUARD_STATEMENTS = (*FUNCTIONS, *(sql for _name, sql in TRIGGERS))

for _sql in GUARD_STATEMENTS:
    event.listen(Base.metadata, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
