"""What an order was discounted by is a record: once the order exists these columns never change (installed with the tables)."""

from sqlalchemy import DDL, event

from app.core.database import Base

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
    ("trg_orders_coupon_frozen",
     "CREATE TRIGGER trg_orders_coupon_frozen BEFORE UPDATE OF discount_cents, coupon_id, coupon_code, coupon_snapshot ON orders FOR EACH ROW EXECUTE FUNCTION coupon_fields_frozen()"),
    ("trg_order_items_discount_frozen",
     "CREATE TRIGGER trg_order_items_discount_frozen BEFORE UPDATE OF discount_cents ON order_items FOR EACH ROW EXECUTE FUNCTION coupon_fields_frozen()"),
)

for _sql in (FUNCTION, *(sql for _n, sql in TRIGGERS)):
    event.listen(Base.metadata, "after_create", DDL(_sql.replace("%", "%%")).execute_if(dialect="postgresql"))
