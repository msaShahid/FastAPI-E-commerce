"""
Phase 2.9: ownership constraints, bounded recovery of early webhook events, malformed events, authorization of every
payment-related route, and secret hygiene. Real PostgreSQL and HTTP; real Stripe signature verification and parsing;
only outbound Stripe API calls are faked.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.payments.interface import PaymentIntentResult, RefundResult
from app.core.payments.stripe_provider import StripePaymentProvider
from app.main import app
from app.modules.payments.dependencies.payment_deps import get_payment_provider

API = "/api/v1"
WHSEC = "whsec_p29_distinctive_secret"
SK = "sk_test_p29_distinctive_key"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


def sign(payload: bytes, secret: str = WHSEC) -> str:
    ts = int(time.time())
    return f"t={ts},v1={hmac.new(secret.encode(), f'{ts}.'.encode() + payload, hashlib.sha256).hexdigest()}"


def event_body(kind, obj, event_id):
    return json.dumps({"id": event_id, "object": "event", "type": kind, "data": {"object": obj}}).encode()


class P29Stripe(StripePaymentProvider):
    """The REAL Stripe provider (real signature check and parsing); only outbound calls are faked, with predictable ids."""

    def __init__(self):
        super().__init__(secret_key=SK, webhook_secret=WHSEC)
        self.created, self.refund_calls = [], []

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        self.created.append({"amount": amount_cents, "currency": currency})
        return PaymentIntentResult(f"pi_p29_{len(self.created)}", "secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        return PaymentIntentResult(provider_payment_id, "secret", "canceled")

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        self.refund_calls.append({"amount": amount_cents})
        return RefundResult(f"re_p29_{len(self.refund_calls)}", amount_cents, "succeeded", currency)


@pytest.fixture
def provider(monkeypatch):
    from app.modules.payments.dependencies import payment_deps

    p = P29Stripe()
    app.dependency_overrides[get_payment_provider] = lambda: p
    monkeypatch.setattr(payment_deps, "get_payment_provider", lambda: p)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", SK)
    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", WHSEC)
    return p


@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "P29", "description": None}, headers=admin_headers)).json()["id"]


_n = {"i": 0, "e": 0}


async def make_order(client, admin_headers, category, qty=2):
    _n["i"] += 1
    i = _n["i"]
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"P29 {i}", "description": None, "price_cents": 1000, "sku": f"P29-{i}", "stock": 10,
        "category_id": category, "status": "active"})).json()
    await client.post(f"{API}/auth/register", json={"username": f"p29_{i}", "email": f"p29{i}@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": f"p29{i}@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": qty}, headers=h)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": f"p29-{i}"})
    assert r.status_code == 201, r.text
    return h, r.json(), p


async def send(client, kind, obj, event_id=None):
    _n["e"] += 1
    body = event_body(kind, obj, event_id or f"evt_p29_{_n['e']}")
    return await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body)})


def succeeded(pi, amount=2000):
    return {"id": pi, "amount": amount, "amount_received": amount, "currency": "usd"}


async def one(db, sql, **params):
    await db.rollback()
    return (await db.execute(text(sql), params)).scalar_one()


async def run_recovery(session_factory):
    from app.modules.payments.jobs import reprocess_deferred_events

    await reprocess_deferred_events({}, session_factory)


async def make_due(db):
    await db.execute(text("UPDATE deferred_webhook_events SET next_attempt_at = now() - interval '1 second' WHERE status = 'pending'"))
    await db.commit()


# ================================================================================================ XOR constraints
async def _user_id(client, db):
    await client.post(f"{API}/auth/register", json={"username": "xor_user", "email": "xor@example.com", "password": "UserPass123!"})
    return await one(db, "SELECT id::text FROM users WHERE email = 'xor@example.com'")


ORDER_INS = ("INSERT INTO orders (id, user_id, guest_token, guest_email, idempotency_key, subtotal_cents, shipping_cents, "
             "tax_cents, total_cents, status) VALUES (gen_random_uuid(), :u, :t, :e, :k, 100, 0, 0, 100, 'pending')")
CART_INS = "INSERT INTO carts (user_id, guest_token) VALUES (:u, :t)"


async def test_the_ownership_constraints_exist_in_the_database(client, db_session):
    names = {r[0] for r in (await db_session.execute(text(
        "SELECT conname FROM pg_constraint WHERE conname IN ('ck_orders_user_xor_guest', 'ck_carts_user_xor_guest')"))).all()}
    assert names == {"ck_orders_user_xor_guest", "ck_carts_user_xor_guest"}


async def test_an_order_belongs_to_exactly_one_of_a_user_or_a_guest(client, db_session):
    uid = await _user_id(client, db_session)
    ok = [dict(u=uid, t=None, e=None, k="o1"), dict(u=None, t="tok-1", e="g@example.com", k="o2")]
    for params in ok:
        await db_session.execute(text(ORDER_INS), params)
    await db_session.commit()
    bad = [dict(u=uid, t="tok-2", e="g@example.com", k="b1"),   # both owners
           dict(u=None, t=None, e=None, k="b2"),                 # no owner
           dict(u=None, t="tok-3", e=None, k="b3"),              # a guest without an email
           dict(u=None, t=None, e="g@example.com", k="b4"),      # an email without a guest token
           dict(u=uid, t=None, e="g@example.com", k="b5")]       # a user order carrying guest data
    for params in bad:
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(text(ORDER_INS), params)
        assert "ck_orders_user_xor_guest" in str(exc.value)
        await db_session.rollback()


async def test_a_cart_belongs_to_exactly_one_of_a_user_or_a_guest(client, db_session):
    uid = await _user_id(client, db_session)
    await db_session.execute(text(CART_INS), {"u": uid, "t": None})
    await db_session.execute(text(CART_INS), {"u": None, "t": "cart-tok"})
    await db_session.commit()
    for params in ({"u": uid, "t": "cart-tok-2"}, {"u": None, "t": None}):
        with pytest.raises(DBAPIError) as exc:
            await db_session.execute(text(CART_INS), params)
        assert "ck_carts_user_xor_guest" in str(exc.value)
        await db_session.rollback()


async def test_real_checkouts_for_users_and_guests_satisfy_the_constraints(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)                      # a registered user's order and cart
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})            # a guest cart
    g = await client.post(f"{API}/orders/checkout", json={"guest_email": "guest@example.com", "shipping_address": ADDR},
                          headers={"Idempotency-Key": "p29-guest"})
    assert g.status_code == 201, g.text
    assert await one(db_session, "SELECT count(*) FROM orders WHERE user_id IS NULL AND guest_token IS NOT NULL AND guest_email IS NOT NULL") == 1
    assert await one(db_session, "SELECT count(*) FROM orders WHERE user_id IS NOT NULL AND guest_token IS NULL AND guest_email IS NULL") == 1


# ================================================================================================ bounded recovery of early events
async def test_an_event_that_beats_its_payment_record_is_recovered_once_and_closes_its_own_reconciliation(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)
    assert (await send(client, "payment_intent.succeeded", succeeded("pi_p29_1"), "evt_early")).status_code == 200   # no payment row yet
    assert await one(db_session, "SELECT status FROM deferred_webhook_events") == "pending"
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert kinds == ["unmatched_payment"]
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"          # nothing guessed
    assert (await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)).status_code == 201         # the row appears (pi_p29_1)
    await make_due(db_session)
    await run_recovery(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert (await db_session.execute(text("SELECT status, attempts FROM deferred_webhook_events"))).one() == ("recovered", 1)
    assert await one(db_session, "SELECT status FROM payment_reconciliations WHERE kind = 'unmatched_payment'") == "resolved"
    assert "Recovered automatically" in await one(db_session, "SELECT resolution_note FROM payment_reconciliations WHERE kind = 'unmatched_payment'")
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'webhook.deferred_event_recovered'") == 1
    # idempotent: running again, and the provider re-delivering the same event, change nothing
    await run_recovery(test_session_factory)
    assert (await send(client, "payment_intent.succeeded", succeeded("pi_p29_1"), "evt_early")).status_code == 200
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'PAID'", o=o["id"]) == 1
    assert await one(db_session, "SELECT count(*) FROM background_jobs WHERE job_type = 'send_order_confirmation'") == 1


async def test_recovery_is_bounded_by_attempts_and_by_the_window_and_leaves_the_case_for_a_human(
    client, admin_headers, category, provider, db_session, test_session_factory, monkeypatch
):
    monkeypatch.setattr(get_settings(), "webhook_recovery_max_attempts", 2)
    await send(client, "payment_intent.succeeded", succeeded("pi_never"), "evt_never")
    for _ in range(2):
        await make_due(db_session)
        await run_recovery(test_session_factory)
    row = (await db_session.execute(text("SELECT status, attempts, last_reason FROM deferred_webhook_events"))).one()
    assert row[0] == "expired" and row[1] == 2 and "never appeared" in row[2]
    await make_due(db_session)
    await run_recovery(test_session_factory)                                             # no further processing, ever
    assert await one(db_session, "SELECT attempts FROM deferred_webhook_events") == 2
    assert await one(db_session, "SELECT status FROM payment_reconciliations WHERE kind = 'unmatched_payment'") == "open"
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'webhook.deferred_event_expired'") == 1
    # the time window bounds it independently of the attempt count
    monkeypatch.setattr(get_settings(), "webhook_recovery_max_attempts", 99)
    await send(client, "payment_intent.succeeded", succeeded("pi_late_window"), "evt_window")
    await db_session.execute(text("UPDATE deferred_webhook_events SET next_attempt_at = now() - interval '1 second', "
                                  "expires_at = now() - interval '1 minute' WHERE event_id = 'evt_window'"))
    await db_session.commit()
    await run_recovery(test_session_factory)
    assert await one(db_session, "SELECT status FROM deferred_webhook_events WHERE event_id = 'evt_window'") == "expired"


async def test_a_recovered_payment_for_a_cancelled_order_is_only_reconciled_never_reopened_or_fulfilled(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category, qty=2)
    await send(client, "payment_intent.succeeded", succeeded("pi_p29_1"), "evt_cancelled_order")
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)).status_code == 200
    await make_due(db_session)
    await run_recovery(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"
    kinds = sorted(r[0] for r in (await db_session.execute(text("SELECT kind FROM payment_reconciliations"))).all())
    assert "late_payment" in kinds
    assert await one(db_session, "SELECT count(*) FROM refunds") == 0 and provider.refund_calls == []
    assert await one(db_session, "SELECT stock FROM products WHERE id = :p", p=p["id"]) == 10                       # restocked once, by the cancel


async def test_a_recovered_event_with_the_wrong_amount_never_marks_the_order_paid(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)
    await send(client, "payment_intent.succeeded", succeeded("pi_p29_1", amount=1), "evt_wrong_amount")
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    await make_due(db_session)
    await run_recovery(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await one(db_session, "SELECT status::text FROM payments") == "pending"
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'amount_mismatch'") == 1


async def test_concurrent_recovery_workers_apply_an_event_once(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p = await make_order(client, admin_headers, category)
    await send(client, "payment_intent.succeeded", succeeded("pi_p29_1"), "evt_concurrent")
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    await make_due(db_session)
    await asyncio.gather(*[run_recovery(test_session_factory) for _ in range(4)])
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'PAID'", o=o["id"]) == 1
    assert await one(db_session, "SELECT count(*) FROM background_jobs WHERE job_type = 'send_order_confirmation'") == 1
    assert await one(db_session, "SELECT attempts FROM deferred_webhook_events") == 1


async def test_an_early_dispute_is_recorded_once_its_payment_exists(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p = await make_order(client, admin_headers, category)
    dispute = {"id": "dp_early", "payment_intent": "pi_p29_1", "amount": 2000, "currency": "usd", "status": "needs_response", "reason": "fraudulent"}
    await send(client, "charge.dispute.created", dispute, "evt_dp_early")
    assert await one(db_session, "SELECT count(*) FROM disputes") == 0
    await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    await send(client, "payment_intent.succeeded", succeeded("pi_p29_1"))
    await make_due(db_session)
    await run_recovery(test_session_factory)
    assert await one(db_session, "SELECT status FROM disputes") == "open"
    assert await one(db_session, "SELECT status FROM payment_reconciliations WHERE kind = 'unmatched_dispute'") == "resolved"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"             # a dispute changes no order


async def test_recovery_can_be_disabled_and_stores_no_payload(client, admin_headers, provider, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_recovery_interval_seconds", 0)
    await send(client, "payment_intent.succeeded", succeeded("pi_x"), "evt_off")
    assert await one(db_session, "SELECT count(*) FROM deferred_webhook_events") == 0                                # not kept...
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'unmatched_payment'") == 1  # ...but not lost
    monkeypatch.setattr(get_settings(), "webhook_recovery_interval_seconds", 60)
    await send(client, "payment_intent.succeeded", succeeded("pi_y"), "evt_on")
    cols = {r[0] for r in (await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'deferred_webhook_events'"))).all()}
    assert not cols & {"payload", "body", "raw", "headers", "signature", "customer", "card", "email"}
    dump = (await db_session.execute(text("SELECT row_to_json(d)::text FROM deferred_webhook_events d"))).scalar_one()
    assert WHSEC not in dump and SK not in dump and "t=" not in dump
    listed = await client.get(f"{API}/admin/webhook-events/deferred", headers=admin_headers)
    assert listed.status_code == 200 and listed.json()["items"][0]["event_id"] == "evt_on"


# ================================================================================================ malformed events
@pytest.mark.parametrize("body", [
    b'{"id": "evt_bad_1", "type": "payment_intent.succeeded"}',                       # signed, but no data
    b'{"id": "evt_bad_2", "type": "payment_intent.succeeded", "data": {"object": null}}',
    b'{"id": "evt_bad_3", "type": "payment_intent.succeeded", "data": {"object": {}}}',  # no object id
    b"not json at all",
    b"[]",
])
async def test_a_validly_signed_but_malformed_event_is_a_400_not_a_retried_500(client, provider, db_session, body):
    r = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body)})
    assert r.status_code == 400, r.text
    assert WHSEC not in r.text and SK not in r.text
    assert await one(db_session, "SELECT count(*) FROM processed_webhook_events") == 0           # nothing was claimed


async def test_stale_timestamps_bad_signatures_and_unknown_providers_are_refused_without_leaking_secrets(client, provider, caplog):
    body = event_body("payment_intent.succeeded", succeeded("pi_z"), "evt_refuse")
    old = int(time.time()) - 3600
    stale = f"t={old},v1={hmac.new(WHSEC.encode(), f'{old}.'.encode() + body, hashlib.sha256).hexdigest()}"
    with caplog.at_level(logging.DEBUG):
        assert (await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": stale})).status_code == 400
        assert (await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body, "whsec_other")})).status_code == 400
        assert (await client.post(f"{API}/payments/webhooks/paypal", content=body, headers={"Stripe-Signature": sign(body)})).status_code == 404
    assert WHSEC not in caplog.text and SK not in caplog.text and "whsec_other" not in caplog.text


# ================================================================================================ authorization
ADMIN_ROUTES = [
    ("GET", "/admin/refunds"), ("GET", "/admin/reconciliations"), ("GET", "/admin/disputes"),
    ("GET", "/admin/webhook-events"), ("GET", "/admin/webhook-events/deferred"), ("GET", "/admin/lifecycle/health"),
    ("GET", "/admin/orders/unknown-currency"), ("GET", "/admin/tax/readiness"),
    ("POST", f"/admin/reconciliations/{uuid.uuid4()}/resolve"), ("POST", f"/admin/reconciliations/{uuid.uuid4()}/refund"),
    ("POST", f"/admin/orders/{uuid.uuid4()}/cancel-and-refund"), ("POST", f"/admin/orders/{uuid.uuid4()}/establish-currency"),
    ("POST", f"/payments/{uuid.uuid4()}/refund"), ("PATCH", f"/orders/{uuid.uuid4()}/status"), ("PUT", "/store/currency"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_every_privileged_payment_route_refuses_anonymous_and_customer_callers(client, admin_headers, provider, method, path):
    await client.post(f"{API}/auth/register", json={"username": "plain_p29", "email": "plain29@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": "plain29@example.com", "password": "UserPass123!"})).json()["access_token"]
    body = {"json": {"status": "fulfilled"}} if method in ("PATCH", "PUT") else {}
    anon = await client.request(method, f"{API}{path}", **body)
    cust = await client.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {tok}"}, **body)
    assert anon.status_code in (401, 403), (method, path, anon.status_code)
    assert cust.status_code == 403, (method, path, cust.status_code)


def test_no_public_route_can_change_a_payment_or_refund_directly():
    # The OpenAPI schema is the reliable route table (FastAPI wraps included routers, so app.routes is not flat).
    paths = app.openapi()["paths"]
    payment_paths = {p: sorted(m.upper() for m in ops) for p, ops in paths.items() if "/payments" in p.lower()}
    assert payment_paths, "the route table was not read: this test would pass vacuously"
    assert payment_paths == {
        "/api/v1/payments": ["POST"],                                   # create a payment for the caller's own order
        "/api/v1/payments/webhook": ["POST"],                           # the provider, authenticated by signature only
        "/api/v1/payments/webhooks/{provider}": ["POST"],
        "/api/v1/payments/{order_id}/refund": ["POST"],                 # admin: REQUEST a refund
    }, payment_paths                                                    # nothing edits or deletes a payment record
    mutating_status = {p: m for p, m in ((p, sorted(x.upper() for x in ops)) for p, ops in paths.items())
                       if p.endswith("/status")}
    assert mutating_status == {"/api/v1/orders/{order_id}/status": ["PATCH"]}   # the one admin status route


def test_graphql_exposes_no_payment_refund_or_order_status_mutation():
    from app.graphql.schema import schema

    mutation = schema.get_type_by_name("Mutation")
    names = [f.name.lower() for f in mutation.fields] if mutation is not None else []
    assert not [n for n in names if any(w in n for w in ("payment", "refund", "cancel", "status", "dispute", "paid"))], names


async def test_a_customer_cannot_mark_an_order_paid_by_any_route(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)
    for status in ("paid", "refunded", "cancelled", "fulfilled"):
        r = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": status}, headers=h)
        assert r.status_code == 403
        r2 = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": status}, headers=admin_headers)
        assert r2.status_code in (409, 422)                              # not even an admin can fabricate it by hand
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"


# ================================================================================================ secret hygiene
async def test_secrets_never_appear_in_api_responses_audit_records_or_logs(client, admin_headers, category, provider, db_session, test_session_factory, caplog):
    secrets = [SK, WHSEC, "whsec_other_p29"]
    seen = []
    with caplog.at_level(logging.DEBUG):
        h, o, p = await make_order(client, admin_headers, category)
        seen.append((await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)).text)
        seen.append((await send(client, "payment_intent.succeeded", succeeded("pi_p29_1"))).text)
        body = event_body("payment_intent.succeeded", succeeded("pi_p29_1"), "evt_secret_probe")
        seen.append((await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body, "whsec_other_p29")})).text)
        seen.append((await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).text)
        for path in ("/admin/refunds", "/admin/reconciliations", "/admin/disputes", "/admin/webhook-events",
                     "/admin/webhook-events/deferred", "/admin/lifecycle/health", "/admin/tax/readiness", "/store/currency",
                     "/admin/orders/unknown-currency"):
            seen.append((await client.get(f"{API}{path}", headers=admin_headers)).text)
    audit = (await db_session.execute(text("SELECT coalesce(string_agg(details::text, ' '), '') FROM audit_logs"))).scalar_one()
    events = (await db_session.execute(text("SELECT coalesce(string_agg(row_to_json(e)::text, ' '), '') FROM processed_webhook_events e"))).scalar_one()
    for secret in secrets:
        assert not any(secret in text_ for text_ in seen), secret
        assert secret not in audit and secret not in events and secret not in caplog.text, secret
