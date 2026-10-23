"""
Policy decisions: INR default, legacy-currency remediation, paid-order cancellation with refund, late payments,
full-refund-only, and the configurable unpaid-order timeout. Real PostgreSQL, real HTTP, real worker code paths.
"""

import asyncio
import json

import pytest
from _store_currency import reset_store_currency
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import Settings, get_settings
from app.core.jobs.schedule import enqueue_due
from app.core.payments.exceptions import PaymentProviderError, PaymentProviderTimeoutError
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)
from app.main import app
from app.modules.audit.models.audit_log import AuditLog
from app.modules.payments.dependencies.payment_deps import get_payment_provider

API = "/api/v1"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


class PolicyProvider(PaymentProvider):
    def __init__(self):
        self.created, self.cancel_calls, self.refund_calls, self.ledger = [], [], [], {}
        self.cancel_mode, self.refund_mode, self.last_pi, self.intents = "canceled", "succeeded", None, {}

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        self.last_pi = f"pi_pol_{len(self.created) + 1}"
        self.created.append({"amount": amount_cents, "currency": currency})
        self.intents[self.last_pi] = {"amount": amount_cents, "currency": currency}
        return PaymentIntentResult(self.last_pi, "secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        self.cancel_calls.append(provider_payment_id)
        if self.cancel_mode == "timeout":
            raise PaymentProviderTimeoutError()
        if self.cancel_mode == "error":
            raise PaymentProviderError("boom")
        return PaymentIntentResult(provider_payment_id, "secret", self.cancel_mode)

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        self.refund_calls.append({"key": idempotency_key, "amount": amount_cents, "currency": currency})
        entry = self.ledger.setdefault(idempotency_key, {"id": f"re_pol_{len(self.ledger) + 1}", "status": self.refund_mode})
        return RefundResult(entry["id"], amount_cents, entry["status"], currency)

    def verify_and_parse_webhook(self, *, payload, signature_header):
        d = json.loads(payload)
        intent = self.intents.get(d["pi"], {})
        return WebhookEvent(
            d["event_id"], d["type"], d["pi"], d.get("amount", intent.get("amount")), None, d.get("currency", intent.get("currency")),
            d.get("refund_id"), d.get("refund_status"), d.get("dispute_id"), d.get("dispute_status"), d.get("dispute_reason"),
        )


@pytest.fixture
def provider(monkeypatch):
    from app.modules.payments.dependencies import payment_deps

    p = PolicyProvider()
    app.dependency_overrides[get_payment_provider] = lambda: p
    monkeypatch.setattr(payment_deps, "get_payment_provider", lambda: p)
    return p


@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "POL", "description": None}, headers=admin_headers)).json()["id"]


_n = {"i": 0}


async def make_order(client, admin_headers, category, *, qty=2, stock=10):
    _n["i"] += 1
    i = _n["i"]
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"POL {i}", "description": None, "price_cents": 1000, "sku": f"POL-{i}", "stock": stock,
        "category_id": category, "status": "active"})).json()
    await client.post(f"{API}/auth/register", json={"username": f"pol_{i}", "email": f"pol{i}@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": f"pol{i}@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": qty}, headers=h)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": f"pol-{i}"})
    assert r.status_code == 201, r.text
    return h, r.json(), p


_e = {"i": 0}


async def hook(client, pi, kind="payment_intent.succeeded", **extra):
    _e["i"] += 1
    body = json.dumps({"event_id": f"evt_pol_{_e['i']}", "type": kind, "pi": pi, **extra})
    return await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=x"})


async def paid(client, admin_headers, category, provider, **kw):
    h, o, p = await make_order(client, admin_headers, category, **kw)
    assert (await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)).status_code == 201
    pi = provider.last_pi
    assert (await hook(client, pi)).status_code == 200
    return h, o, p, pi


async def jobs(session_factory):
    import app.jobs  # noqa: F401
    from app.core.jobs.worker import run_one

    n = 0
    while await run_one(session_factory) and n < 30:
        n += 1


async def one(db, sql, **params):
    await db.rollback()
    return (await db.execute(text(sql), params)).scalar_one()


async def stock(client, pid):
    return (await client.get(f"{API}/products/{pid}")).json()["stock"]


async def forget_currency(db, order_id):
    """Make an order a LEGACY order: its currency was never recorded."""
    await db.execute(text("ALTER TABLE orders DISABLE TRIGGER USER"))
    await db.execute(text("UPDATE orders SET currency = NULL WHERE id = :o"), {"o": order_id})
    await db.execute(text("ALTER TABLE orders ENABLE TRIGGER USER"))
    await db.commit()


async def reasons(client, admin_headers, **params):
    return (await client.get(f"{API}/admin/reconciliations", params=params, headers=admin_headers)).json()["items"]


# ================================================================================================ A. INR default
async def test_a_new_store_defaults_to_inr_persists_it_and_it_never_silently_changes(client, admin_headers, category, db_session, monkeypatch):
    await db_session.execute(text("DELETE FROM store_settings"))   # a brand-new store: nothing configured, no orders
    await db_session.commit()
    h, o, p = await make_order(client, admin_headers, category)
    assert o["currency"] == "inr" and o["tax_snapshot"]["currency"] == "inr"
    assert await one(db_session, "SELECT currency FROM store_settings") == "inr"          # the default is now fixed
    monkeypatch.setattr(get_settings(), "default_store_currency", "eur")                  # changing the default later...
    h2, o2, _ = await make_order(client, admin_headers, category)
    assert o2["currency"] == "inr"                                                         # ...changes nothing for this store
    assert (await client.get(f"{API}/store/currency")).json()["configured"] is True


async def test_the_default_is_configuration_not_a_hardcoded_assumption(client, admin_headers, category, db_session, monkeypatch):
    await db_session.execute(text("DELETE FROM store_settings"))
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "default_store_currency", "jpy")
    h, o, p = await make_order(client, admin_headers, category)
    assert o["currency"] == "jpy" and o["tax_snapshot"]["minor_unit_exponent"] == 0     # arithmetic follows the currency


def test_the_default_currency_must_be_supported():
    base = dict(_env_file=None, database_url="postgresql+asyncpg://x", jwt_secret="x" * 32,
                stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    assert Settings(**base).default_store_currency == "inr"
    assert Settings(**base, default_store_currency="USD").default_store_currency == "usd"
    with pytest.raises(ValidationError):
        Settings(**base, default_store_currency="xyz")


async def test_an_administrator_choosing_the_currency_sees_whether_payments_would_work(client, admin_headers, monkeypatch):
    for code, enabled, expect in (("kwd", "", "not verified"), ("usd", "inr", "PAYMENT_CURRENCIES_ENABLED"), ("inr", "inr", None)):
        await reset_store_currency(code)  # one fresh store per currency: a set currency is fixed for the life of the store
        monkeypatch.setattr(get_settings(), "payment_currencies_enabled", enabled)
        r = await client.put(f"{API}/store/currency", json={"currency": code}, headers=admin_headers)
        assert r.status_code == 200 and r.json()["configured"] is True
        if expect:
            assert any(expect in w for w in r.json()["warnings"])
        else:
            assert r.json()["warnings"] == []
    assert (await client.put(f"{API}/store/currency", json={"currency": "xyz"}, headers=admin_headers)).status_code == 422


# ================================================================================================ 5. legacy currency
async def test_the_report_lists_only_orders_with_no_recorded_currency_and_says_why_payments_are_not_proof(
    client, admin_headers, category, provider, db_session
):
    h, legacy, p = await make_order(client, admin_headers, category)
    await client.post(f"{API}/payments", json={"order_id": legacy["id"]}, headers=h)
    h2, known, _ = await make_order(client, admin_headers, category)
    await forget_currency(db_session, legacy["id"])
    body = (await client.get(f"{API}/admin/orders/unknown-currency", headers=admin_headers)).json()
    assert [i["order_id"] for i in body["items"]] == [legacy["id"]] and body["total"] == 1
    item = body["items"][0]
    assert item["total_cents"] == legacy["total_cents"] and item["payments"][0]["currency"] == "usd"
    assert "NOT reliable evidence" in item["caveat"]
    assert (await client.get(f"{API}/admin/orders/unknown-currency", headers=h)).status_code == 403


async def test_payments_and_refunds_are_blocked_until_the_legacy_currency_is_established(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await forget_currency(db_session, o["id"])
    refund = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert refund.status_code == 422 and refund.json()["code"] == "order_currency_unknown"
    cancel = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    assert cancel.status_code == 422 and cancel.json()["code"] == "order_currency_unknown"
    assert provider.refund_calls == [] and await one(db_session, "SELECT count(*) FROM refunds") == 0
    h2, o2, _ = await make_order(client, admin_headers, category)
    await forget_currency(db_session, o2["id"])
    pay = await client.post(f"{API}/payments", json={"order_id": o2["id"]}, headers=h2)
    assert pay.status_code == 422 and "no recorded currency" in pay.json()["detail"]


async def test_establishing_a_legacy_currency_needs_evidence_cannot_contradict_a_payment_and_converts_nothing(
    client, admin_headers, category, provider, db_session
):
    h, o, p, pi = await paid(client, admin_headers, category, provider)        # its payment was recorded in usd
    await forget_currency(db_session, o["id"])
    url = f"{API}/admin/orders/{o['id']}/establish-currency"
    body = {"currency": "usd", "evidence_type": "provider_record", "evidence_reference": f"Stripe dashboard, payment {pi}"}
    assert (await client.post(url, json=body, headers=h)).status_code == 403                                   # admin only
    for bad in ({**body, "evidence_type": "guess"}, {**body, "evidence_reference": "x"}, {**body, "currency": "xyz"},
                {k: v for k, v in body.items() if k != "evidence_reference"}):
        assert (await client.post(url, json=bad, headers=admin_headers)).status_code == 422
    conflict = await client.post(url, json={**body, "currency": "inr"}, headers=admin_headers)                  # contradicts the payment
    assert conflict.status_code == 409 and conflict.json()["code"] == "currency_conflicts_with_payment"
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None
    total_before = await one(db_session, "SELECT total_cents FROM orders WHERE id = :o", o=o["id"])
    ok = await client.post(url, json=body, headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["currency"] == "usd"
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) == "usd"
    assert await one(db_session, "SELECT total_cents FROM orders WHERE id = :o", o=o["id"]) == total_before   # nothing converted
    est = (await db_session.execute(text("SELECT evidence_type, evidence_reference FROM order_currency_remediations WHERE decision = 'established'"))).one()
    assert tuple(est) == ("provider_record", f"Stripe dashboard, payment {pi}")
    audit = await one(db_session, "SELECT details->>'evidence_reference' FROM audit_logs WHERE action = 'order.currency_established'")
    assert audit == f"Stripe dashboard, payment {pi}"
    again = await client.post(url, json=body, headers=admin_headers)
    assert again.status_code == 409 and again.json()["code"] == "order_currency_known"
    with pytest.raises(DBAPIError):                                   # and it can never change afterwards
        await db_session.execute(text("UPDATE orders SET currency = 'inr' WHERE id = :o"), {"o": o["id"]})
    await db_session.rollback()
    assert (await client.get(f"{API}/admin/orders/unknown-currency", headers=admin_headers)).json()["total"] == 0


async def test_once_established_the_legacy_order_can_be_refunded_normally(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await forget_currency(db_session, o["id"])
    await client.post(f"{API}/admin/orders/{o['id']}/establish-currency", headers=admin_headers, json={
        "currency": "usd", "evidence_type": "accounting_record", "evidence_reference": f"Ledger entry for order {o['id']}"})
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"


# ================================================================================================ 1. paid-order cancellation
async def test_a_paid_order_is_cancelled_through_a_refund_and_stock_returns_exactly_once_after_confirmation(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=3, stock=10)
    assert await stock(client, p["id"]) == 7
    first = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    second = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)   # retry
    assert first.status_code == second.status_code == 202 and first.json()["id"] == second.json()["id"]
    assert first.json()["cancels_order"] is True and first.json()["status"] == "requested"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"       # nothing yet
    assert await stock(client, p["id"]) == 7 and provider.refund_calls == []
    await jobs(test_session_factory)
    await jobs(test_session_factory)                                                                          # duplicate job runs
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    assert await stock(client, p["id"]) == 10 and len(provider.refund_calls) == 1                            # restocked once
    assert not await one(db_session, "SELECT stock_released_at IS NULL FROM orders WHERE id = :o", o=o["id"])
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'order.cancel_refund_requested'") == 1


async def test_the_ordinary_cancel_and_the_admin_status_change_still_cannot_cancel_a_paid_order(client, admin_headers, category, provider):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)).json()["code"] == "cancellation_not_allowed"
    patch = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "cancelled"}, headers=admin_headers)
    assert patch.status_code == 409 and patch.json()["code"] == "status_change_not_allowed"
    assert await stock(client, p["id"]) == 8


@pytest.mark.parametrize("state", ["pending", "fulfilled"])
async def test_only_a_paid_unfulfilled_order_uses_cancel_and_refund(client, admin_headers, category, provider, state):
    if state == "pending":
        h, o, p = await make_order(client, admin_headers, category)
    else:
        h, o, p, pi = await paid(client, admin_headers, category, provider)
        assert (await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)).status_code == 200
    r = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "cancel_and_refund_not_allowed"
    assert (await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=h)).status_code == 403


async def test_refunding_a_fulfilled_order_never_restocks_it(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=2, stock=10)
    await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await stock(client, p["id"]) == 8                      # the goods shipped: no automatic restock


async def test_the_refund_must_be_provider_confirmed_before_the_order_changes_or_stock_returns(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=2, stock=10)
    provider.refund_mode = "pending"
    await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status FROM refunds") == "pending"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid" and await stock(client, p["id"]) == 8
    rid = await one(db_session, "SELECT provider_refund_id FROM refunds")
    for _ in range(3):  # the confirmation, delivered repeatedly
        assert (await hook(client, pi, kind="refund.updated", refund_id=rid, refund_status="succeeded",
                           amount=o["total_cents"], currency="usd")).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await stock(client, p["id"]) == 10                     # once


async def test_a_failed_refund_changes_nothing_and_the_cancellation_can_be_requested_again(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=2, stock=10)
    provider.refund_mode = "failed"
    await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status FROM refunds") == "failed"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid" and await stock(client, p["id"]) == 8
    provider.refund_mode = "succeeded"
    again = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    assert again.status_code == 202
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded" and await stock(client, p["id"]) == 10


async def test_an_order_cannot_be_fulfilled_while_its_cancellation_refund_is_in_flight(client, admin_headers, category, provider):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    r = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "status_change_not_allowed"


async def test_a_plain_refund_in_flight_cannot_be_turned_into_a_cancellation(client, admin_headers, category, provider):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    r = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "refund_already_requested"


async def test_an_open_dispute_blocks_cancel_and_refund(client, admin_headers, category, provider):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await hook(client, pi, kind="charge.dispute.created", dispute_id="dp_pol", dispute_status="needs_response", amount=2000, currency="usd")
    r = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "payment_disputed"


async def test_concurrent_cancel_and_refund_requests_make_one_refund_and_restock_once(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=2, stock=10)
    results = await asyncio.gather(*[client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers) for _ in range(4)])
    assert [r.status_code for r in results] == [202] * 4 and len({r.json()["id"] for r in results}) == 1
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT count(*) FROM refunds") == 1 and len(provider.refund_calls) == 1
    assert await stock(client, p["id"]) == 10


# ================================================================================================ 2. late payments
async def test_a_payment_after_expiry_is_reconciled_not_fulfilled_not_refunded_and_stock_returns_once(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category, qty=2, stock=10)
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    pi = provider.last_pi
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '3 hours' WHERE id = :o"), {"o": o["id"]})
    await db_session.commit()
    await enqueue_due(test_session_factory, get_settings())
    await jobs(test_session_factory)                                              # the order expires and is restocked
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled" and await stock(client, p["id"]) == 10
    for _ in range(2):                                                            # the money arrives late (and is re-delivered)
        assert (await hook(client, pi)).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"   # not reopened
    assert await stock(client, p["id"]) == 10                                                                  # not restocked again
    assert [i["kind"] for i in await reasons(client, admin_headers)] == ["late_payment"]
    assert await one(db_session, "SELECT count(*) FROM refunds") == 0 and provider.refund_calls == []        # not auto-refunded
    assert await one(db_session, "SELECT count(*) FROM orders") == 1                                          # no duplicate order


# ================================================================================================ 4. timeout and provider outcomes
async def test_the_timeout_is_configuration_used_by_the_sweep(client, admin_headers, category, provider, db_session, test_session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "pending_order_timeout_minutes", 5)
    h, old, po = await make_order(client, admin_headers, category)
    h2, young, py = await make_order(client, admin_headers, category)
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '10 minutes' WHERE id = :o"), {"o": old["id"]})
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '3 minutes' WHERE id = :o"), {"o": young["id"]})
    await db_session.commit()
    await enqueue_due(test_session_factory, get_settings())
    await jobs(test_session_factory)
    states = {r[0]: r[1] for r in (await db_session.execute(text("SELECT id::text, status::text FROM orders"))).all()}
    assert (states[old["id"]], states[young["id"]]) == ("cancelled", "pending")


async def test_the_providers_cancellation_outcome_is_recorded(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)).status_code == 200
    outcomes = await one(db_session, "SELECT details->'provider_outcomes' FROM audit_logs WHERE action = 'order.cancelled'")
    assert outcomes == [{"provider_payment_id": provider.last_pi, "outcome": "canceled"}]


async def test_a_payment_that_succeeds_during_expiry_opens_a_case_and_the_order_is_confirmed_by_its_webhook(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category, qty=2, stock=10)
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    pi = provider.last_pi
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '3 hours' WHERE id = :o"), {"o": o["id"]})
    await db_session.commit()
    provider.cancel_mode = "succeeded"                                 # the provider says: this payment already went through
    await enqueue_due(test_session_factory, get_settings())
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"      # NOT expired
    assert await stock(client, p["id"]) == 8                                                                    # still reserved
    assert "cancel_payment_succeeded" in [i["kind"] for i in await reasons(client, admin_headers)]
    assert (await hook(client, pi)).status_code == 200                 # its webhook arrives
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert await stock(client, p["id"]) == 8


async def test_an_unreachable_provider_during_expiry_never_expires_the_order(client, admin_headers, category, provider, db_session, test_session_factory):
    from app.modules.payments.jobs import expire_order

    h, o, p = await make_order(client, admin_headers, category)
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '3 hours' WHERE id = :o"), {"o": o["id"]})
    await db_session.commit()
    provider.cancel_mode = "timeout"
    with pytest.raises(RuntimeError):
        await expire_order({"order_id": o["id"]}, test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending" and await stock(client, p["id"]) == 8
    assert [i["kind"] for i in await reasons(client, admin_headers)] == ["cancel_uncertain"]


# ================================================================================================ 3. refund limits under concurrency
async def test_concurrent_full_refund_requests_never_exceed_the_payment(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    results = await asyncio.gather(*[client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers) for _ in range(5)])
    assert [r.status_code for r in results] == [202] * 5 and len({r.json()["id"] for r in results}) == 1
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT sum(amount_cents) FROM refunds") == await one(db_session, "SELECT amount_cents FROM payments")
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"


# ================================================================================================ confirmed decision 1
async def test_an_administrator_cannot_cancel_a_paid_order_through_the_ordinary_cancel_either(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=2, stock=10)
    r = await client.post(f"{API}/orders/{o['id']}/cancel", headers=admin_headers)       # even as an ADMIN
    assert r.status_code == 409 and r.json()["code"] == "cancellation_not_allowed"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert await stock(client, p["id"]) == 8 and provider.cancel_calls == [] and provider.refund_calls == []


async def test_a_customer_cannot_cancel_or_refund_their_own_paid_order_by_any_route(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)).status_code == 409
    assert (await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=h)).status_code == 403
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=h)).status_code == 403
    assert (await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "cancelled"}, headers=h)).status_code == 403
    assert (await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund")).status_code in (401, 403)
    assert await one(db_session, "SELECT count(*) FROM refunds") == 0 and await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"


async def test_an_admin_cancellation_records_who_asked_and_follows_the_refund_inventory_and_audit_procedure(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider, qty=2, stock=10)
    admin_id = (await client.get(f"{API}/auth/me", headers=admin_headers)).json()["id"]
    r = await client.post(f"{API}/admin/orders/{o['id']}/cancel-and-refund", headers=admin_headers)
    assert r.status_code == 202
    assert str(await one(db_session, "SELECT requested_by_user_id::text FROM refunds")) == admin_id
    assert await one(db_session, "SELECT actor_user_id::text FROM audit_logs WHERE action = 'order.cancel_refund_requested'") == admin_id
    assert await one(db_session, "SELECT actor_user_id::text FROM audit_logs WHERE action = 'refund.requested'") == admin_id
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)).status_code == 409     # the customer still cannot
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded" and await stock(client, p["id"]) == 10
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'refund.succeeded'") == 1


# ================================================================================================ confirmed decision 2
def review(**over):
    base = {"decision": "established", "currency": "usd", "evidence_type": "provider_record", "evidence_reference": "x"}
    return {**base, **over}


async def legacy(client, admin_headers, category, provider, db_session, *, with_payment=True):
    h, o, p = await make_order(client, admin_headers, category)
    pi = None
    if with_payment:
        await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
        pi = provider.last_pi
    await forget_currency(db_session, o["id"])
    return h, o, pi


async def reviews_of(db, order_id):
    return [tuple(r) for r in (await db.execute(text(
        "SELECT decision, currency, evidence_type, reviewer, reviewed FROM (SELECT decision, currency, evidence_type, "
        "established_by_user_id IS NOT NULL AS reviewer, created_at IS NOT NULL AS reviewed, created_at, id "
        "FROM order_currency_remediations WHERE order_id = :o ORDER BY created_at, id) t"), {"o": order_id})).all()]


async def test_evidence_that_does_not_concern_this_order_leaves_the_currency_unresolved_and_is_recorded(
    client, admin_headers, category, provider, db_session
):
    h, o, pi = await legacy(client, admin_headers, category, provider, db_session)
    other_h, other, other_pi = await legacy(client, admin_headers, category, provider, db_session)   # a DIFFERENT order's payment
    url = f"{API}/admin/orders/{o['id']}/establish-currency"
    for body, code in (
        (review(evidence_reference=f"Stripe dashboard, payment {other_pi}"), "evidence_not_specific_to_order"),   # another order's id
        (review(evidence_reference="the Stripe dashboard shows usd"), "evidence_not_specific_to_order"),          # cites nothing
        (review(evidence_type="accounting_record", evidence_reference="Ledger 2026, usd"), "evidence_not_specific_to_order"),
    ):
        r = await client.post(url, json=body, headers=admin_headers)
        assert r.status_code == 422 and r.json()["code"] == code, r.text
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None                  # still unresolved
    assert [r[0] for r in await reviews_of(db_session, o["id"])] == ["unresolved_inconclusive"] * 3                 # every attempt is on record
    assert all(r[3] and r[4] for r in await reviews_of(db_session, o["id"]))                                         # reviewer and time recorded
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'order.currency_review_unresolved'") == 3
    item = next(i for i in (await client.get(f"{API}/admin/orders/unknown-currency", headers=admin_headers)).json()["items"] if i["order_id"] == o["id"])
    assert item["manual_review_required"] is True and len(item["reviews"]) == 3
    assert "manual review required" in item["reviews"][0]["remediation"] and item["reviews"][0]["reviewer_user_id"]
    refund = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert refund.status_code == 422 and refund.json()["code"] == "order_currency_unknown"                           # still blocked


async def test_specific_evidence_establishes_the_currency_and_the_review_is_fully_recorded(client, admin_headers, category, provider, db_session):
    h, o, pi = await legacy(client, admin_headers, category, provider, db_session)
    ok = await client.post(f"{API}/admin/orders/{o['id']}/establish-currency", headers=admin_headers,
                           json=review(evidence_reference=f"Stripe dashboard, payment {pi.upper()}", note="Checked against the Stripe export"))
    assert ok.status_code == 200
    body = ok.json()
    assert (body["decision"], body["currency"], body["evidence_type"]) == ("established", "usd", "provider_record")
    assert body["remediation"] == "Order currency set to usd" and body["reviewer_user_id"] and body["reviewed_at"]
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) == "usd"
    # accounting evidence works the same way when it cites the order id
    h2, o2, _ = await legacy(client, admin_headers, category, provider, db_session, with_payment=False)
    ok2 = await client.post(f"{API}/admin/orders/{o2['id']}/establish-currency", headers=admin_headers,
                            json=review(evidence_type="accounting_record", evidence_reference=f"Invoice ledger, order {o2['id']}"))
    assert ok2.status_code == 200 and ok2.json()["currency"] == "usd"


async def test_a_provider_record_needs_a_payment_to_cite(client, admin_headers, category, provider, db_session):
    h, o, _ = await legacy(client, admin_headers, category, provider, db_session, with_payment=False)    # an unpaid legacy order
    r = await client.post(f"{API}/admin/orders/{o['id']}/establish-currency", headers=admin_headers,
                          json=review(evidence_reference="Stripe dashboard, some charge ch_999"))
    assert r.status_code == 422 and r.json()["code"] == "evidence_not_specific_to_order"
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None


async def test_conflicting_evidence_is_recorded_as_conflicting_and_changes_nothing(client, admin_headers, category, provider, db_session):
    h, o, pi = await legacy(client, admin_headers, category, provider, db_session)        # the payment row says usd
    total = await one(db_session, "SELECT total_cents FROM orders WHERE id = :o", o=o["id"])
    r = await client.post(f"{API}/admin/orders/{o['id']}/establish-currency", headers=admin_headers,
                          json=review(currency="inr", evidence_reference=f"Stripe dashboard, payment {pi}"))
    assert r.status_code == 409 and r.json()["code"] == "currency_conflicts_with_payment"
    assert [r[0] for r in await reviews_of(db_session, o["id"])] == ["unresolved_conflicting"]
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None
    assert await one(db_session, "SELECT total_cents FROM orders WHERE id = :o", o=o["id"]) == total
    assert await one(db_session, "SELECT currency FROM payments") == "usd"                  # the payment record is untouched


async def test_store_history_alone_is_accepted_only_when_unambiguous(client, admin_headers, category, provider, db_session):
    h, o, _ = await legacy(client, admin_headers, category, provider, db_session, with_payment=False)
    url = f"{API}/admin/orders/{o['id']}/establish-currency"
    hist = lambda **k: review(evidence_type="store_history_reviewed", evidence_reference="Store settings reviewed 2026-10", **k)  # noqa: E731
    not_attested = await client.post(url, json=hist(), headers=admin_headers)
    assert not_attested.status_code == 422 and not_attested.json()["code"] == "store_history_ambiguous"
    different = await client.post(url, json=hist(currency="inr", store_history_unambiguous=True), headers=admin_headers)   # store is usd now
    assert different.status_code == 422 and different.json()["code"] == "store_history_ambiguous"
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None
    ok = await client.post(url, json=hist(store_history_unambiguous=True), headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["currency"] == "usd"
    assert [r[0] for r in await reviews_of(db_session, o["id"])] == ["unresolved_inconclusive", "unresolved_inconclusive", "established"]


async def test_store_history_is_ambiguous_if_the_store_currency_was_ever_changed(client, admin_headers, category, provider, db_session):
    # A change is on record from before currencies became fixed (the API can no longer produce one, an old deployment can have it).
    db_session.add(AuditLog(actor_user_id=None, action="store.currency_set", target_type="store_settings", target_id="1",
                            details={"from": "inr", "to": "usd"}))
    await db_session.commit()
    h, o, _ = await legacy(client, admin_headers, category, provider, db_session, with_payment=False)
    r = await client.post(f"{API}/admin/orders/{o['id']}/establish-currency", headers=admin_headers,
                          json=review(evidence_type="store_history_reviewed", evidence_reference="Store settings reviewed",
                                      store_history_unambiguous=True))
    assert r.status_code == 422 and "changed" in r.json()["detail"]                                # ...so history alone cannot settle it
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None


async def test_a_reviewer_can_record_that_the_evidence_is_inconclusive_and_the_order_stays_for_manual_review(
    client, admin_headers, category, provider, db_session
):
    h, o, pi = await legacy(client, admin_headers, category, provider, db_session)
    url = f"{API}/admin/orders/{o['id']}/establish-currency"
    base = {"decision": "unresolved", "evidence_type": "provider_record", "evidence_reference": "Stripe export 2026-09"}
    assert (await client.post(url, json=base, headers=admin_headers)).status_code == 422                                  # needs reason + note
    assert (await client.post(url, json={**base, "reason": "inconclusive"}, headers=admin_headers)).status_code == 422
    assert (await client.post(url, json={**base, "reason": "inconclusive", "note": "x", "currency": "usd"}, headers=admin_headers)).status_code == 422
    ok = await client.post(url, json={**base, "reason": "conflicting", "note": "Export shows two currencies"}, headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["decision"] == "unresolved_conflicting" and ok.json()["currency"] is None
    assert await one(db_session, "SELECT currency FROM orders WHERE id = :o", o=o["id"]) is None
    # it does not stop a LATER review that has proper evidence
    later = await client.post(url, json=review(evidence_reference=f"Stripe dashboard, payment {pi}"), headers=admin_headers)
    assert later.status_code == 200 and later.json()["currency"] == "usd"
    assert [r[0] for r in await reviews_of(db_session, o["id"])] == ["unresolved_conflicting", "established"]
    assert (await client.post(url, json=review(evidence_reference=f"payment {pi}"), headers=admin_headers)).json()["code"] == "order_currency_known"


async def test_only_administrators_can_review_legacy_currency(client, admin_headers, category, provider, db_session):
    h, o, pi = await legacy(client, admin_headers, category, provider, db_session)
    url = f"{API}/admin/orders/{o['id']}/establish-currency"
    assert (await client.post(url, json=review(evidence_reference=f"payment {pi}"), headers=h)).status_code == 403
    assert (await client.post(url, json=review(evidence_reference=f"payment {pi}"))).status_code in (401, 403)
    assert await one(db_session, "SELECT count(*) FROM order_currency_remediations") == 0


async def test_the_database_enforces_the_decision_and_currency_pairing_and_one_established_review(client, admin_headers, category, provider, db_session):
    h, o, pi = await legacy(client, admin_headers, category, provider, db_session)
    ins = ("INSERT INTO order_currency_remediations (id, order_id, decision, currency, evidence_type, evidence_reference) "
           "VALUES (gen_random_uuid(), :o, :d, :c, 'provider_record', 'ref')")
    for d, c in (("established", None), ("unresolved_conflicting", "usd"), ("guess", None)):
        with pytest.raises(DBAPIError):
            await db_session.execute(text(ins), {"o": o["id"], "d": d, "c": c})
        await db_session.rollback()
    await db_session.execute(text(ins), {"o": o["id"], "d": "established", "c": "usd"})
    await db_session.commit()
    with pytest.raises(DBAPIError):                                          # a second ESTABLISHED review for the same order
        await db_session.execute(text(ins), {"o": o["id"], "d": "established", "c": "usd"})
    await db_session.rollback()
    await db_session.execute(text(ins), {"o": o["id"], "d": "unresolved_inconclusive", "c": None})   # unresolved ones may repeat
    await db_session.execute(text(ins), {"o": o["id"], "d": "unresolved_inconclusive", "c": None})
    await db_session.commit()
