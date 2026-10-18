"""Phase 2.6: currency and money correctness. Fictional values only; no live Stripe."""

import importlib.util
import json
import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)
from app.main import app
from app.modules.audit.models.audit_log import AuditLog
from app.modules.payments.dependencies.payment_deps import get_payment_provider
from app.modules.store.models.guard import CURRENCY_FUNCTIONS

API = "/api/v1"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


class RecordingProvider(PaymentProvider):
    """Like Stripe: reports what each intent was created with; tests can override per event."""

    def __init__(self):
        self.created = []
        self.intents = {}
        self.refund_calls = []
        self.refund_override = {}

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        pid = f"pi_cur_{len(self.created) + 1}"
        self.created.append({"amount": amount_cents, "currency": currency})
        self.intents[pid] = {"amount": amount_cents, "currency": currency}
        return PaymentIntentResult(pid, f"{pid}_secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    def verify_and_parse_webhook(self, *, payload, signature_header):
        d = json.loads(payload)
        intent = self.intents.get(d["pi"], {})
        return WebhookEvent(d["event_id"], d["type"], d["pi"], d.get("amount", intent.get("amount")),
                            d.get("amount_received"), d.get("currency", intent.get("currency")))

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        self.refund_calls.append({"amount_cents": amount_cents, "currency": currency})
        return RefundResult("re_1", self.refund_override.get("amount", amount_cents), "succeeded",
                            self.refund_override.get("currency", currency))


@pytest.fixture
def provider():
    p = RecordingProvider()
    app.dependency_overrides[get_payment_provider] = lambda: p
    return p


@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "Cur Cat", "description": None},
                              headers=admin_headers)).json()["id"]


async def product(client, h, category, price, sku="CUR-1", stock=20):
    r = await client.post(f"{API}/products", headers=h, json={
        "name": f"Item {sku}", "description": None, "price_cents": price, "sku": sku, "stock": stock,
        "category_id": category, "status": "active"})
    assert r.status_code == 201, r.text
    return r.json()


async def set_store_currency(client, h, code):
    r = await client.put(f"{API}/store/currency", json={"currency": code}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def user_order(client, product_id, key="cur-1"):
    await client.post(f"{API}/auth/register",
                      json={"username": "buyer_user", "email": "buyer@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "buyer@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": product_id, "quantity": 1}, headers=h)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": key})
    assert r.status_code == 201, r.text
    return h, r.json()


async def pay(client, h, order_id):
    return await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)


async def webhook(client, pi, event_id="evt_1", **extra):
    body = json.dumps({"event_id": event_id, "type": "payment_intent.succeeded", "pi": pi, **extra})
    return await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=x"})


async def audit_actions(db, prefix="payment."):
    return [a for (a,) in (await db.execute(select(AuditLog.action).where(AuditLog.action.like(f"{prefix}%")))).all()]


async def order_status(db, order_id):
    return (await db.execute(text("SELECT status FROM orders WHERE id = :o"), {"o": order_id})).scalar_one()


# ------------------------------------------------------------------ store currency
async def test_public_store_currency_reports_the_currency_and_exponent(client, db_session):
    r = (await client.get(f"{API}/store/currency")).json()
    assert r == {"currency": "usd", "minor_unit_exponent": 2, "payment_verified": True}
    await db_session.execute(text("DELETE FROM store_settings"))
    await db_session.commit()
    assert (await client.get(f"{API}/store/currency")).json() == {
        "currency": None, "minor_unit_exponent": None, "payment_verified": None}


async def test_only_an_administrator_can_set_the_store_currency(client, admin_headers):
    assert (await client.put(f"{API}/store/currency", json={"currency": "inr"})).status_code in (401, 403)
    await client.post(f"{API}/auth/register",
                      json={"username": "plain_user", "email": "plain@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "plain@example.com", "password": "UserPass123!"})).json()["access_token"]
    assert (await client.put(f"{API}/store/currency", json={"currency": "inr"},
                             headers={"Authorization": f"Bearer {tok}"})).status_code == 403


async def test_store_currency_can_be_set_and_changed_before_any_order_and_is_audited(client, admin_headers, db_session):
    assert (await set_store_currency(client, admin_headers, "INR"))["currency"] == "inr"
    assert (await set_store_currency(client, admin_headers, "jpy"))["minor_unit_exponent"] == 0
    kwd = await set_store_currency(client, admin_headers, "kwd")
    assert kwd["minor_unit_exponent"] == 3 and kwd["payment_verified"] is False
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action == "store.currency_set"))).scalars().all()
    assert [(r.details["from"], r.details["to"]) for r in rows][:2] == [("usd", "inr"), ("inr", "jpy")]


@pytest.mark.parametrize("bad", ["xyz", "dollars", "us", "12"])
async def test_unsupported_currencies_cannot_be_set(client, admin_headers, bad):
    r = await client.put(f"{API}/store/currency", json={"currency": bad}, headers=admin_headers)
    assert r.status_code == 422
    if len(bad) == 3:
        assert r.json()["code"] == "currency_unsupported"
    assert (await client.get(f"{API}/store/currency")).json()["currency"] == "usd"


async def test_store_currency_is_locked_once_an_order_exists(client, admin_headers, category, db_session):
    p = await product(client, admin_headers, category, 1000)
    await user_order(client, p["id"])
    r = await client.put(f"{API}/store/currency", json={"currency": "inr"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "store_currency_locked"
    assert (await set_store_currency(client, admin_headers, "usd"))["currency"] == "usd"  # same value: fine
    # and the database refuses it even if the service is bypassed
    for sql in ("UPDATE store_settings SET currency = 'inr'", "DELETE FROM store_settings"):
        with pytest.raises(IntegrityError):
            await db_session.execute(text(sql))
        await db_session.rollback()


async def test_checkout_without_a_store_currency_is_refused_and_changes_nothing(client, admin_headers, category, db_session):
    p = await product(client, admin_headers, category, 1000)
    await db_session.execute(text("DELETE FROM store_settings"))
    await db_session.commit()
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})
    r = await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR},
                          headers={"Idempotency-Key": "no-cur"})
    assert r.status_code == 503 and r.json()["code"] == "store_currency_not_configured"
    assert (await db_session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0
    assert (await client.get(f"{API}/products/{p['id']}")).json()["stock"] == 20


# ------------------------------------------------------------------ order currency, snapshots, history
async def test_an_order_records_its_currency_and_the_snapshots_agree(client, admin_headers, category):
    p = await product(client, admin_headers, category, 1000)
    _, o = await user_order(client, p["id"])
    assert o["currency"] == "usd"
    assert o["shipping_snapshot"]["currency"] == o["tax_snapshot"]["currency"] == "usd"
    assert o["shipping_snapshot"]["minor_unit_exponent"] == o["tax_snapshot"]["minor_unit_exponent"] == 2


async def test_an_orders_currency_cannot_change_and_history_is_stable(client, admin_headers, category, db_session):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    with pytest.raises(IntegrityError):
        await db_session.execute(text("UPDATE orders SET currency = 'inr' WHERE id = :o"), {"o": o["id"]})
    await db_session.rollback()
    assert (await client.get(f"{API}/orders/{o['id']}", headers=h)).json()["currency"] == "usd"
    # setting the store currency again (refused) can never reinterpret the stored order
    assert (await client.put(f"{API}/store/currency", json={"currency": "jpy"}, headers=admin_headers)).status_code == 409


async def test_a_legacy_order_without_a_currency_cannot_be_paid(client, admin_headers, category, db_session, provider):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    await db_session.execute(text("ALTER TABLE orders DISABLE TRIGGER USER"))
    await db_session.execute(text("UPDATE orders SET currency = NULL WHERE id = :o"), {"o": o["id"]})
    await db_session.execute(text("ALTER TABLE orders ENABLE TRIGGER USER"))
    await db_session.commit()
    r = await pay(client, h, o["id"])
    assert r.status_code == 422 and "no recorded currency" in r.json()["detail"] and provider.created == []


# ------------------------------------------------------------------ zero-, two- and three-decimal checkouts
@pytest.mark.parametrize(("code", "exponent", "price", "tax_pct", "tax", "shipping"), [
    ("jpy", 0, 1000, "10", 100, 300),      # 0 decimals: 1000 is 1,000 yen
    ("usd", 2, 1999, "18", 360, 300),      # 1999 * 0.18 = 359.82 -> 360
    ("kwd", 3, 12345, "18", 2222, 300),    # 12.345 KWD: 12345 * 0.18 = 2222.1 -> 2222
    ("inr", 2, 2505, "5", 125, 300),       # 125.25 -> 125 (half-up on .25)
])
async def test_totals_reconcile_in_minor_units_for_every_exponent(
    client, admin_headers, category, monkeypatch, code, exponent, price, tax_pct, tax, shipping
):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", shipping)
    monkeypatch.setattr(s, "tax_rate_percent", Decimal(tax_pct))
    await set_store_currency(client, admin_headers, code)
    p = await product(client, admin_headers, category, price, sku=f"EXP-{code}")
    _, o = await user_order(client, p["id"])
    assert o["currency"] == code and o["tax_snapshot"]["minor_unit_exponent"] == exponent
    assert (o["subtotal_cents"], o["shipping_cents"], o["tax_cents"]) == (price, shipping, tax)
    assert o["total_cents"] == price + shipping + tax and isinstance(o["total_cents"], int)
    assert o["tax_snapshot"]["tax_paise"] == o["tax_cents"] and o["shipping_snapshot"]["applied_paise"] == shipping


# ------------------------------------------------------------------ payment creation
@pytest.mark.parametrize(("code", "price"), [("usd", 1999), ("inr", 250000), ("jpy", 1100), ("eur", 5), ("gbp", 99)])
async def test_payments_use_the_orders_currency_and_the_exact_minor_unit_amount(
    client, admin_headers, category, provider, db_session, code, price
):
    await set_store_currency(client, admin_headers, code)
    p = await product(client, admin_headers, category, price, sku=f"PAY-{code}")
    h, o = await user_order(client, p["id"])
    r = await pay(client, h, o["id"])
    assert r.status_code == 201, r.text
    assert provider.created == [{"amount": o["total_cents"], "currency": code}]   # no hard-coded USD, no scaling
    row = (await db_session.execute(text("SELECT amount_cents, currency FROM payments"))).one()
    assert tuple(row) == (o["total_cents"], code)


@pytest.mark.parametrize("code", ["kwd", "bhd", "krw"])
async def test_unverified_currencies_can_be_ordered_but_never_paid(client, admin_headers, category, provider, db_session, code):
    await set_store_currency(client, admin_headers, code)
    p = await product(client, admin_headers, category, 1000, sku=f"UNV-{code}")
    h, o = await user_order(client, p["id"])
    r = await pay(client, h, o["id"])
    assert r.status_code == 503 and r.json()["code"] == "payment_currency_not_verified"
    assert provider.created == []
    assert (await db_session.execute(text("SELECT count(*) FROM payments"))).scalar_one() == 0


@pytest.mark.parametrize("enabled", ["", "eur", "inr,gbp"])
async def test_a_currency_the_client_has_not_enabled_is_refused(client, admin_headers, category, provider, monkeypatch, enabled):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])  # store currency is usd
    monkeypatch.setattr(get_settings(), "payment_currencies_enabled", enabled)
    r = await pay(client, h, o["id"])
    assert r.status_code == 503 and r.json()["code"] == "payment_currency_not_enabled" and provider.created == []


async def test_an_existing_payment_that_disagrees_with_the_order_is_never_reused(
    client, admin_headers, category, provider, db_session
):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    assert (await pay(client, h, o["id"])).status_code == 201
    await db_session.execute(text("ALTER TABLE payments DISABLE TRIGGER USER"))
    await db_session.execute(text("UPDATE payments SET currency = 'eur'"))
    await db_session.execute(text("ALTER TABLE payments ENABLE TRIGGER USER"))
    await db_session.commit()
    r = await pay(client, h, o["id"])
    assert r.status_code == 422 and r.json()["code"] == "payment_inconsistent" and len(provider.created) == 1


# ------------------------------------------------------------------ webhook verification
async def test_a_matching_webhook_marks_the_order_paid(client, admin_headers, category, provider, db_session):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    await pay(client, h, o["id"])
    assert (await webhook(client, "pi_cur_1")).status_code == 200
    assert await order_status(db_session, o["id"]) == "paid"


@pytest.mark.parametrize(("extra", "action"), [
    ({"amount": 1}, "payment.amount_mismatch"),
    ({"currency": "eur"}, "payment.amount_mismatch"),
    ({"amount_received": 1}, "payment.amount_mismatch"),
    ({"amount": None}, "payment.unverifiable_success"),
    ({"currency": None}, "payment.unverifiable_success"),
])
async def test_a_webhook_that_does_not_match_is_never_treated_as_success(
    client, admin_headers, category, provider, db_session, extra, action
):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    await pay(client, h, o["id"])
    r = await webhook(client, "pi_cur_1", **extra)
    assert r.status_code == 200  # acknowledged so Stripe stops retrying; handled for review
    assert await order_status(db_session, o["id"]) == "pending"
    assert (await db_session.execute(text("SELECT status FROM payments"))).scalar_one() == "pending"
    assert action in await audit_actions(db_session)
    await webhook(client, "pi_cur_1", **extra)  # replay: deduplicated, no second audit entry
    assert (await audit_actions(db_session)).count(action) == 1


async def test_the_mismatch_audit_entry_says_what_was_expected_and_received(client, admin_headers, category, provider, db_session):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    await pay(client, h, o["id"])
    await webhook(client, "pi_cur_1", amount=1, currency="eur")
    d = (await db_session.execute(select(AuditLog.details).where(AuditLog.action == "payment.amount_mismatch"))).scalar_one()
    assert (d["expected_amount"], d["expected_currency"], d["received_amount"], d["received_currency"]) == (
        o["total_cents"], "usd", 1, "eur")


# ------------------------------------------------------------------ refunds
async def _paid_order(client, admin_headers, category, provider, db_session, code="usd", price=1999):
    await set_store_currency(client, admin_headers, code)
    p = await product(client, admin_headers, category, price, sku=f"REF-{code}")
    h, o = await user_order(client, p["id"])
    await pay(client, h, o["id"])
    assert (await webhook(client, "pi_cur_1")).status_code == 200
    return h, o


@pytest.mark.parametrize(("code", "price"), [("usd", 1999), ("jpy", 1100), ("inr", 250000)])
async def test_refunds_use_the_original_payment_currency_and_amount(client, admin_headers, category, provider, db_session, code, price):
    _, o = await _paid_order(client, admin_headers, category, provider, db_session, code, price)
    r = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert provider.refund_calls == [{"amount_cents": o["total_cents"], "currency": code}]
    assert await order_status(db_session, o["id"]) == "refunded"


async def test_a_refund_for_an_order_whose_currency_disagrees_with_its_payment_is_refused(
    client, admin_headers, category, provider, db_session
):
    _, o = await _paid_order(client, admin_headers, category, provider, db_session)
    await db_session.execute(text("ALTER TABLE orders DISABLE TRIGGER USER"))
    await db_session.execute(text("UPDATE orders SET currency = 'eur' WHERE id = :o"), {"o": o["id"]})
    await db_session.execute(text("ALTER TABLE orders ENABLE TRIGGER USER"))
    await db_session.commit()
    r = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "payment_inconsistent" and provider.refund_calls == []


async def test_a_refund_that_differs_from_what_was_requested_is_recorded_for_review(
    client, admin_headers, category, provider, db_session
):
    _, o = await _paid_order(client, admin_headers, category, provider, db_session)
    provider.refund_override = {"amount": o["total_cents"] - 1}
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 200
    assert "payment.refund_mismatch" in await audit_actions(db_session)


# ------------------------------------------------------------------ database guards and the migration copy
async def test_a_payments_amount_and_currency_cannot_change_after_creation(client, admin_headers, category, provider, db_session):
    p = await product(client, admin_headers, category, 1000)
    h, o = await user_order(client, p["id"])
    await pay(client, h, o["id"])
    for sql in ("UPDATE payments SET amount_cents = 1", "UPDATE payments SET currency = 'eur'"):
        with pytest.raises(IntegrityError):
            await db_session.execute(text(sql))
        await db_session.rollback()
    await db_session.execute(text("UPDATE payments SET status = 'failed'"))  # other columns stay editable
    await db_session.commit()


@pytest.mark.parametrize("sql", [
    "UPDATE orders SET currency = 'USD'",   # not lower-case ISO
    "UPDATE orders SET currency = 'us'",
])
async def test_the_database_rejects_malformed_currency_codes(client, admin_headers, category, db_session, sql):
    p = await product(client, admin_headers, category, 1000)
    await user_order(client, p["id"])
    await db_session.execute(text("ALTER TABLE orders DISABLE TRIGGER USER"))
    with pytest.raises(IntegrityError):
        await db_session.execute(text(sql))
    await db_session.rollback()


def test_migration_and_model_use_identical_currency_guard_sql():
    path = next(pathlib.Path("alembic/versions").glob("e5b7d2a9c418_*.py"))
    spec = importlib.util.spec_from_file_location("currency_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = (module.STORE_CURRENCY_GUARD_SQL, module.ORDER_CURRENCY_GUARD_SQL, module.PAYMENT_AMOUNT_GUARD_SQL)
    assert tuple(s.strip() for s in migration) == tuple(s.strip() for s in CURRENCY_FUNCTIONS)


# ------------------------------------------------------------------ readiness
@pytest.mark.parametrize(("store", "enabled", "codes"), [
    ("usd", "usd", set()),
    ("inr", "inr", set()),                                    # INR is just another currency: no special case
    ("inr", "", {"payment_currency_not_enabled"}),
    ("kwd", "usd", {"payment_currency_not_verified"}),
])
async def test_readiness_reports_payment_currency_blockers(client, admin_headers, monkeypatch, store, enabled, codes):
    await set_store_currency(client, admin_headers, store)
    monkeypatch.setattr(get_settings(), "payment_currencies_enabled", enabled)
    body = (await client.get(f"{API}/admin/tax/readiness", headers=admin_headers)).json()
    assert {b["code"] for b in body["payments"]["blockers"]} == codes
    assert body["payments"]["payment_currency"] == store and body["payments"]["live_payments_enabled"] is False


async def test_readiness_reports_a_missing_store_currency(client, admin_headers, db_session):
    await db_session.execute(text("DELETE FROM store_settings"))
    await db_session.commit()
    body = (await client.get(f"{API}/admin/tax/readiness", headers=admin_headers)).json()
    assert {b["code"] for b in body["payments"]["blockers"]} == {"store_currency_not_configured"}
    assert body["payments"]["payment_currency"] is None


def test_live_payments_and_enabled_currencies_default_to_off():
    s = get_settings()
    assert s.live_payments_enabled is False
