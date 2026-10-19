"""
Database guards for the order/payment lifecycle (defence in depth; the services check first).

orders_lifecycle_guard:
  * status may only follow the documented transitions (pending -> paid | cancelled,
    paid -> fulfilled | refunded, fulfilled -> refunded); cancelled and refunded are final;
  * an order cannot become PAID without a SUCCEEDED payment;
  * an order cannot become REFUNDED without a SUCCEEDED refund;
  * an order cannot become CANCELLED unless its stock has been released;
  * stock_released_at can be set once and never changed (no duplicate restock).
refunds_guard: amount/currency/payment/order/idempotency key never change; succeeded and failed are final.

The same SQL lives in migration f9c3b6e1d752; a test asserts the copies are identical.
"""

ORDERS_LIFECYCLE_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION orders_lifecycle_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.stock_released_at IS NOT NULL AND NEW.stock_released_at IS DISTINCT FROM OLD.stock_released_at THEN
        RAISE EXCEPTION 'stock release cannot be repeated or undone'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT ((OLD.status = 'pending' AND NEW.status IN ('paid', 'cancelled'))
                OR (OLD.status = 'paid' AND NEW.status IN ('fulfilled', 'refunded'))
                OR (OLD.status = 'fulfilled' AND NEW.status = 'refunded')) THEN
            RAISE EXCEPTION 'an order cannot change status from % to %', OLD.status, NEW.status
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'paid'
           AND NOT EXISTS (SELECT 1 FROM payments WHERE order_id = NEW.id AND status = 'succeeded') THEN
            RAISE EXCEPTION 'an order cannot become paid without a succeeded payment'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'refunded'
           AND NOT EXISTS (SELECT 1 FROM refunds WHERE order_id = NEW.id AND status = 'succeeded') THEN
            RAISE EXCEPTION 'an order cannot become refunded without a succeeded refund'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.status = 'cancelled' AND NEW.stock_released_at IS NULL THEN
            RAISE EXCEPTION 'an order cannot be cancelled before its stock is released'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

REFUNDS_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION refunds_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.payment_id IS DISTINCT FROM OLD.payment_id
       OR NEW.order_id IS DISTINCT FROM OLD.order_id
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key THEN
        RAISE EXCEPTION 'what a refund is for cannot be changed after it is created'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('succeeded', 'failed') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'a % refund is final', OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

LIFECYCLE_FUNCTIONS = (ORDERS_LIFECYCLE_GUARD_SQL, REFUNDS_GUARD_SQL)

# trigger name -> (table, timing, function)
LIFECYCLE_TRIGGERS = {
    "trg_orders_lifecycle_guard": ("orders", "BEFORE UPDATE", "orders_lifecycle_guard"),
    "trg_refunds_guard": ("refunds", "BEFORE UPDATE", "refunds_guard"),
}
