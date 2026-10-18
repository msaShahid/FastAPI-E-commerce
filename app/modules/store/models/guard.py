"""
Database guards that keep money meaning stable (defence in depth; the service checks first).

  * store_settings: the store currency cannot be changed or removed once ANY order exists.
  * orders: once an order has a currency it can never change.
  * payments: a payment's amount and currency can never change after creation.

The same SQL lives in migration e5b7d2a9c418; a test asserts the copies are identical.
"""

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

# One statement per entry (the driver executes a single command at a time).
CURRENCY_FUNCTIONS = (STORE_CURRENCY_GUARD_SQL, ORDER_CURRENCY_GUARD_SQL, PAYMENT_AMOUNT_GUARD_SQL)

# trigger name -> (table, timing, function)
CURRENCY_TRIGGERS = {
    "trg_store_settings_currency_guard": ("store_settings", "BEFORE UPDATE OR DELETE", "store_currency_guard"),
    "trg_orders_currency_guard": ("orders", "BEFORE UPDATE OF currency", "orders_currency_guard"),
    "trg_payments_amount_guard": ("payments", "BEFORE UPDATE OF currency, amount_cents", "payments_amount_guard"),
}
