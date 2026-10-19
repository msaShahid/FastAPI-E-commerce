"""
Database guards added in Phase 2.8 (defence in depth; the services check first).

refunds_cumulative_guard: the refunds of one payment (requested, pending, succeeded or uncertain) can never add
  up to more than the payment's amount; the payment row is locked while this is checked, so two refunds racing
  cannot both pass.
disputes_guard: a dispute's payment/amount/currency never change, and a closed dispute (won, lost, closed) is final.

The same SQL lives in migration a2e4c8b6d193; a test asserts the copies are identical.
"""

REFUNDS_CUMULATIVE_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION refunds_cumulative_guard() RETURNS trigger AS $$
DECLARE
    payment_amount integer;
    committed bigint;
BEGIN
    IF NEW.status NOT IN ('requested', 'pending', 'succeeded', 'uncertain') THEN
        RETURN NEW;
    END IF;
    SELECT amount_cents INTO payment_amount FROM payments WHERE id = NEW.payment_id FOR UPDATE;
    SELECT COALESCE(SUM(amount_cents), 0) INTO committed FROM refunds
        WHERE payment_id = NEW.payment_id AND id <> NEW.id
          AND status IN ('requested', 'pending', 'succeeded', 'uncertain');
    IF committed + NEW.amount_cents > payment_amount THEN
        RAISE EXCEPTION 'refunds of a payment cannot exceed the payment amount'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

DISPUTES_GUARD_SQL = r'''CREATE OR REPLACE FUNCTION disputes_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.payment_id IS DISTINCT FROM OLD.payment_id
       OR NEW.amount_cents IS DISTINCT FROM OLD.amount_cents
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.provider IS DISTINCT FROM OLD.provider
       OR NEW.provider_dispute_id IS DISTINCT FROM OLD.provider_dispute_id THEN
        RAISE EXCEPTION 'what a dispute is about cannot be changed'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status IN ('won', 'lost', 'closed') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'a % dispute is final', OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;'''

PROVIDER_FUNCTIONS = (REFUNDS_CUMULATIVE_GUARD_SQL, DISPUTES_GUARD_SQL)

# trigger name -> (table, timing, function)
PROVIDER_TRIGGERS = {
    "trg_refunds_cumulative_guard": ("refunds", "BEFORE INSERT OR UPDATE", "refunds_cumulative_guard"),
    "trg_disputes_guard": ("disputes", "BEFORE UPDATE", "disputes_guard"),
}
