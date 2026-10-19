"""
Phase 2.8: webhook security (REAL Stripe signature verification), event states, reconciliation, refund limits,
disputes and provider readiness. Real PostgreSQL and HTTP; only the outbound Stripe API calls are faked.
"""

import asyncio
import hashlib
import hmac
import importlib.util
import json
import logging
import pathlib
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.payments.exceptions import InvalidWebhookSignatureError
from app.core.payments.interface import PaymentIntentResult, RefundResult
from app.core.payments.stripe_provider import StripePaymentProvider
from app.main import app
from app.modules.payments.dependencies.payment_deps import get_payment_provider
from app.modules.payments.models.provider_guard import PROVIDER_FUNCTIONS
from app.modules.payments.services.lifecycle_service import OrderLifecycleService

API = "/api/v1"
WHSEC = "whsec_unit_test_secret_value"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


def sign(payload: bytes, secret: str = WHSEC, timestamp: int | None = None) -> str:
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def event(kind: str, obj: dict, event_id: str) -> bytes:
    return json.dumps({"id": event_id, "object": "event", "type": kind, "data": {"object": obj}}).encode()


class SignedStripe(StripePaymentProvider):
    """The REAL Stripe provider (real signature verification and event parsing); only outbound API calls are faked."""

    def __init__(self):
        super().__init__(secret_key="sk_test_unit", webhook_secret=WHSEC)
        self.created, self.refund_calls = [], []
        self.last_pi = None

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        self.created.append({"amount": amount_cents, "currency": currency})
        self.last_pi = f"pi_p28_{len(self.created)}"
        return PaymentIntentResult(self.last_pi, "secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        return PaymentIntentResult(provider_payment_id, "secret", "canceled")

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        self.refund_calls.append({"amount": amount_cents, "currency": currency, "key": idempotency_key})
        return RefundResult(f"re_p28_{len(self.refund_calls)}", amount_cents, "succeeded", currency)


@pytest.fixture
def provider(monkeypatch):
    from app.modules.payments.dependencies import payment_deps

    p = SignedStripe()
    app.dependency_overrides[get_payment_provider] = lambda: p
    monkeypatch.setattr(payment_deps, "get_payment_provider", lambda: p)
    return p


@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "P28", "description": None}, headers=admin_headers)).json()["id"]


_n = {"i": 0}


async def make_order(client, admin_headers, category, *, price=1000):
    _n["i"] += 1
    i = _n["i"]
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"P28 {i}", "description": None, "price_cents": price, "sku": f"P28-{i}", "stock": 10,
        "category_id": category, "status": "active"})).json()
    await client.post(f"{API}/auth/register", json={"username": f"p28_{i}", "email": f"p28{i}@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": f"p28{i}@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1}, headers=h)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": f"p28-{i}"})
    assert r.status_code == 201, r.text
    return h, r.json(), p


async def pay(client, h, order_id, provider):
    assert (await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)).status_code == 201
    return provider.last_pi


_e = {"i": 0}


def eid() -> str:
    _e["i"] += 1
    return f"evt_p28_{_e['i']}"


async def send(client, kind, obj, event_id=None, path="/payments/webhook", secret=WHSEC):
    body = event(kind, obj, event_id or eid())
    return await client.post(f"{API}{path}", content=body, headers={"Stripe-Signature": sign(body, secret)})


def succeeded(pi, amount=1000, currency="usd"):
    return {"id": pi, "amount": amount, "amount_received": amount, "currency": currency}


async def paid(client, admin_headers, category, provider, price=1000):
    h, o, p = await make_order(client, admin_headers, category, price=price)
    pi = await pay(client, h, o["id"], provider)
    assert (await send(client, "payment_intent.succeeded", succeeded(pi, price))).status_code == 200
    return h, o, p, pi


async def one(db, sql, **params):
    await db.rollback()
    return (await db.execute(text(sql), params)).scalar_one()


async def jobs(session_factory):
    import app.jobs  # noqa: F401
    from app.core.jobs.worker import run_one

    n = 0
    while await run_one(session_factory) and n < 30:
        n += 1


# ============================================================================================== signatures (real library)
def test_a_correctly_signed_event_is_verified_and_parsed():
    p = StripePaymentProvider(secret_key="sk_test_x", webhook_secret=WHSEC)
    body = event("payment_intent.succeeded", succeeded("pi_1", 1234, "INR"), "evt_1")
    parsed = p.verify_and_parse_webhook(payload=body, signature_header=sign(body))
    assert (parsed.event_id, parsed.event_type, parsed.provider_payment_id, parsed.amount, parsed.currency, parsed.provider) == (
        "evt_1", "payment_intent.succeeded", "pi_1", 1234, "inr", "stripe")


@pytest.mark.parametrize("tamper", ["wrong_secret", "changed_byte", "reserialized_json", "stale_timestamp", "no_v1", "garbage"])
def test_anything_but_the_exact_signed_bytes_is_rejected(tamper):
    p = StripePaymentProvider(secret_key="sk_test_x", webhook_secret=WHSEC)
    body = event("payment_intent.succeeded", succeeded("pi_1"), "evt_1")
    header, payload = sign(body), body
    if tamper == "wrong_secret":
        header = sign(body, secret="whsec_another_secret")
    elif tamper == "changed_byte":
        payload = body.replace(b"1000", b"1001")
    elif tamper == "reserialized_json":  # same data, different bytes: the signature covers the RAW bytes
        payload = json.dumps(json.loads(body), indent=2).encode()
    elif tamper == "stale_timestamp":
        header = sign(body, timestamp=int(time.time()) - 3600)  # replay protection (tolerance is 5 minutes)
    elif tamper == "no_v1":
        header = f"t={int(time.time())}"
    else:
        header = "not-a-signature"
    with pytest.raises(InvalidWebhookSignatureError):
        p.verify_and_parse_webhook(payload=payload, signature_header=header)


async def test_http_webhook_rejects_unsigned_badly_signed_and_oversized_requests(client, provider, caplog):
    body = event("payment_intent.succeeded", succeeded("pi_x"), "evt_http")
    assert (await client.post(f"{API}/payments/webhook", content=body)).status_code == 400                       # no header
    bad = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body, "whsec_wrong")})
    assert bad.status_code == 400
    huge = b"x" * 1_000_001
    assert (await client.post(f"{API}/payments/webhook", content=huge, headers={"Stripe-Signature": "t=1,v1=a"})).status_code == 413
    ok = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body)})
    assert ok.status_code == 200


async def test_secrets_never_appear_in_responses_or_logs(client, provider, caplog):
    body = event("payment_intent.succeeded", succeeded("pi_x"), "evt_leak")
    with caplog.at_level(logging.DEBUG):
        bad = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": sign(body, "whsec_wrong")})
    assert WHSEC not in bad.text and "whsec_wrong" not in bad.text
    assert WHSEC not in caplog.text and "whsec_wrong" not in caplog.text and "sk_test_unit" not in caplog.text


async def test_each_provider_has_its_own_webhook_route_and_unknown_providers_are_404(client, provider):
    body = event("customer.created", {"id": "cus_1"}, "evt_route")
    sig = {"Stripe-Signature": sign(body)}
    assert (await client.post(f"{API}/payments/webhooks/stripe", content=body, headers=sig)).status_code == 200
    assert (await client.post(f"{API}/payments/webhooks/razorpay", content=body, headers=sig)).status_code == 404


# ============================================================================================== duplicates, states, recovery
async def test_a_duplicate_event_changes_nothing_the_second_time(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)
    pi = await pay(client, h, o["id"], provider)
    first = await send(client, "payment_intent.succeeded", succeeded(pi), event_id="evt_dup")
    second = await send(client, "payment_intent.succeeded", succeeded(pi), event_id="evt_dup")
    assert first.status_code == second.status_code == 200
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'PAID'", o=o["id"]) == 1
    row = (await db_session.execute(text("SELECT provider, status, attempts FROM processed_webhook_events WHERE event_id = 'evt_dup'"))).one()
    assert tuple(row) == ("stripe", "processed", 1)


async def test_concurrent_deliveries_of_one_event_apply_its_effects_once(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)
    pi = await pay(client, h, o["id"], provider)
    body = event("payment_intent.succeeded", succeeded(pi), "evt_race")
    hdr = {"Stripe-Signature": sign(body)}
    results = await asyncio.gather(*[client.post(f"{API}/payments/webhook", content=body, headers=hdr) for _ in range(6)])
    assert [r.status_code for r in results] == [200] * 6
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'PAID'", o=o["id"]) == 1
    assert await one(db_session, "SELECT count(*) FROM background_jobs WHERE job_type = 'send_order_confirmation'") == 1
    assert await one(db_session, "SELECT count(*) FROM processed_webhook_events WHERE event_id = 'evt_race'") == 1


async def test_a_failed_attempt_leaves_no_side_effects_is_recorded_and_the_retry_succeeds(
    client, admin_headers, category, provider, db_session, monkeypatch
):
    h, o, p = await make_order(client, admin_headers, category)
    pi = await pay(client, h, o["id"], provider)
    real = OrderLifecycleService.mark_paid
    calls = {"n": 0}

    async def flaky(self, order, payment):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash while marking the order paid")
        return await real(self, order, payment)

    monkeypatch.setattr(OrderLifecycleService, "mark_paid", flaky)
    body = event("payment_intent.succeeded", succeeded(pi), "evt_flaky")
    hdr = {"Stripe-Signature": sign(body)}
    with pytest.raises(RuntimeError):
        await client.post(f"{API}/payments/webhook", content=body, headers=hdr)
    # nothing from the failed attempt persisted...
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await one(db_session, "SELECT status::text FROM payments") == "pending"
    # ...but the failure itself is on record, visible to operators
    row = (await db_session.execute(text("SELECT status, attempts, last_error FROM processed_webhook_events WHERE event_id = 'evt_flaky'"))).one()
    assert row[0] == "failed" and row[1] == 1 and "simulated crash" in row[2]
    listed = (await client.get(f"{API}/admin/webhook-events", params={"status_filter": "failed"}, headers=admin_headers)).json()
    assert [e["event_id"] for e in listed["items"]] == ["evt_flaky"]
    # the provider retries the SAME event: it is recognised as a retry, processed, and counted
    assert (await client.post(f"{API}/payments/webhook", content=body, headers=hdr)).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    row = (await db_session.execute(text("SELECT status, attempts, last_error FROM processed_webhook_events WHERE event_id = 'evt_flaky'"))).one()
    assert tuple(row) == ("processed", 2, None)
    assert (await client.post(f"{API}/payments/webhook", content=body, headers=hdr)).status_code == 200  # a later duplicate
    assert await one(db_session, "SELECT attempts FROM processed_webhook_events WHERE event_id = 'evt_flaky'") == 2


async def test_unsupported_events_are_recorded_for_investigation_not_dropped(client, admin_headers, provider, db_session):
    assert (await send(client, "customer.created", {"id": "cus_1"}, event_id="evt_unsupported")).status_code == 200
    assert (await send(client, "customer.created", {"id": "cus_1"}, event_id="evt_unsupported")).status_code == 200
    row = (await db_session.execute(text("SELECT status, event_type, attempts FROM processed_webhook_events WHERE event_id = 'evt_unsupported'"))).one()
    assert tuple(row) == ("ignored", "customer.created", 1)
    body = (await client.get(f"{API}/admin/webhook-events", params={"status_filter": "ignored"}, headers=admin_headers)).json()
    assert [(e["event_id"], e["event_type"]) for e in body["items"]] == [("evt_unsupported", "customer.created")]
    assert (await client.get(f"{API}/admin/webhook-events")).status_code in (401, 403)


async def test_event_ids_are_scoped_per_provider(client, provider, db_session):
    from app.core.payments.interface import WebhookEvent
    from app.modules.payments.repositories.payment_repository import PaymentRepository

    repo = PaymentRepository(db_session)
    a = WebhookEvent("evt_same", "x", "pi", provider="stripe")
    b = WebhookEvent("evt_same", "x", "pi", provider="razorpay")
    assert await repo.claim_webhook_event(a) and await repo.claim_webhook_event(b)   # same id, different providers
    assert not await repo.claim_webhook_event(a)                                       # same provider: a duplicate



# ============================================================================================== reconciliation: out of order
async def test_a_failure_event_arriving_after_success_never_downgrades_the_payment(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert (await send(client, "payment_intent.payment_failed", {"id": pi})).status_code == 200
    assert await one(db_session, "SELECT status::text FROM payments") == "succeeded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"


async def test_a_refund_event_that_beats_our_refund_job_does_not_cause_a_second_refund(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    # the provider's confirmation arrives BEFORE the worker has run our job
    await send(client, "refund.updated", {"id": "re_early", "payment_intent": pi, "amount": 1000, "currency": "usd", "status": "succeeded"})
    assert await one(db_session, "SELECT status FROM refunds") == "succeeded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    await jobs(test_session_factory)                         # now the job runs: it must see the refund is already final
    assert provider.refund_calls == []                       # the provider was never asked again


async def test_a_dispute_for_an_unknown_payment_is_recorded_not_applied(client, admin_headers, provider):
    r = await send(client, "charge.dispute.created", {"id": "dp_ghost", "payment_intent": "pi_nobody", "amount": 500,
                                                       "currency": "usd", "status": "needs_response"})
    assert r.status_code == 200
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert kinds == ["unmatched_dispute"]


# ============================================================================================== refunds: cumulative limits
async def test_a_partial_refund_made_at_the_provider_is_recorded_and_the_rest_is_what_can_be_refunded(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    partial = {"id": "re_dash_partial", "payment_intent": pi, "amount": 400, "currency": "usd", "status": "succeeded"}
    for _ in range(2):  # the second is a duplicate event
        assert (await send(client, "refund.created", partial)).status_code == 200
    assert await one(db_session, "SELECT count(*) FROM refunds") == 1
    assert (await db_session.execute(text("SELECT origin, status, amount_cents FROM refunds"))).one() == ("provider", "succeeded", 400)
    assert await one(db_session, "SELECT refunded_amount_cents FROM payments") == 400
    assert await one(db_session, "SELECT status::text FROM payments") == "succeeded"          # only partly refunded
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert kinds == ["external_refund"]
    # asking NovaCart to refund now refunds exactly what is left
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert provider.refund_calls[0]["amount"] == 600
    assert await one(db_session, "SELECT refunded_amount_cents FROM payments") == 1000
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"


async def test_a_refund_that_would_exceed_the_payment_is_never_recorded(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await send(client, "refund.created", {"id": "re_ok", "payment_intent": pi, "amount": 700, "currency": "usd", "status": "succeeded"})
    await send(client, "refund.created", {"id": "re_too_much", "payment_intent": pi, "amount": 700, "currency": "usd", "status": "succeeded"})
    assert await one(db_session, "SELECT count(*) FROM refunds") == 1 and await one(db_session, "SELECT refunded_amount_cents FROM payments") == 700
    kinds = sorted(i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"])
    assert kinds == ["external_refund", "refund_mismatch"]


async def test_a_provider_refund_in_another_currency_is_not_applied(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await send(client, "refund.created", {"id": "re_eur", "payment_intent": pi, "amount": 100, "currency": "eur", "status": "succeeded"})
    assert await one(db_session, "SELECT count(*) FROM refunds") == 0
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'refund_mismatch'") == 1


async def test_the_database_enforces_the_cumulative_refund_limit_and_one_in_flight_refund(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    pid = await one(db_session, "SELECT id::text FROM payments")
    insert = ("INSERT INTO refunds (id, payment_id, order_id, amount_cents, currency, idempotency_key, status, origin) "
              "VALUES (gen_random_uuid(), :p, :o, :a, 'usd', :k, :s, :g)")
    await db_session.execute(text(insert), {"p": pid, "o": o["id"], "a": 600, "k": "k1", "s": "succeeded", "g": "provider"})
    await db_session.commit()
    for args in ({"a": 500, "k": "k2", "s": "succeeded", "g": "provider"},     # 600 + 500 > 1000
                 {"a": 500, "k": "k3", "s": "requested", "g": "requested"}):   # also counts while in flight
        with pytest.raises(DBAPIError):
            await db_session.execute(text(insert), {"p": pid, "o": o["id"], **args})
        await db_session.rollback()
    await db_session.execute(text(insert), {"p": pid, "o": o["id"], "a": 400, "k": "k4", "s": "requested", "g": "requested"})
    await db_session.commit()                                                  # exactly the remainder is allowed
    with pytest.raises(DBAPIError):                                            # a second in-flight refund of ours
        await db_session.execute(text(insert), {"p": pid, "o": o["id"], "a": 0 + 1, "k": "k5", "s": "requested", "g": "requested"})
    await db_session.rollback()


async def test_the_unique_index_alone_allows_only_one_in_flight_refund_we_started(client, admin_headers, category, provider, db_session):
    """Amounts stay far below the payment so the CUMULATIVE limit is not what stops the second one."""
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    pid = await one(db_session, "SELECT id::text FROM payments")
    insert = ("INSERT INTO refunds (id, payment_id, order_id, amount_cents, currency, idempotency_key, status, origin) "
              "VALUES (gen_random_uuid(), :p, :o, 100, 'usd', :k, :s, :g)")
    await db_session.execute(text(insert), {"p": pid, "o": o["id"], "k": "u1", "s": "requested", "g": "requested"})
    await db_session.commit()
    with pytest.raises(DBAPIError) as exc:
        await db_session.execute(text(insert), {"p": pid, "o": o["id"], "k": "u2", "s": "pending", "g": "requested"})
    assert "ux_refunds_one_inflight_per_payment" in str(exc.value)
    await db_session.rollback()
    # a provider-origin refund is not "ours in flight": it only counts toward the cumulative total
    await db_session.execute(text(insert), {"p": pid, "o": o["id"], "k": "u3", "s": "succeeded", "g": "provider"})
    await db_session.commit()


async def test_a_fully_refunded_payment_cannot_be_refunded_again(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await send(client, "refund.created", {"id": "re_full", "payment_intent": pi, "amount": 1000, "currency": "usd", "status": "succeeded"})
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"   # recorded, order untouched
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 422


# ============================================================================================== disputes
def dispute(pi, status="needs_response", dp="dp_1", amount=1000):
    return {"id": dp, "payment_intent": pi, "amount": amount, "currency": "usd", "status": status, "reason": "fraudulent"}


async def test_a_dispute_is_recorded_audited_and_asks_a_human_without_touching_the_order(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    for _ in range(2):  # created, then a redelivery
        assert (await send(client, "charge.dispute.created", dispute(pi))).status_code == 200
    row = (await db_session.execute(text("SELECT status, provider_status, reason, amount_cents FROM disputes"))).one()
    assert tuple(row) == ("open", "needs_response", "fraudulent", 1000)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert await one(db_session, "SELECT status::text FROM payments") == "succeeded"
    assert await one(db_session, "SELECT stock FROM products WHERE id = :p", p=p["id"]) == 9
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert kinds == ["dispute_opened"]
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'dispute.opened'") == 1
    listed = (await client.get(f"{API}/admin/disputes", headers=admin_headers)).json()
    assert [d["status"] for d in listed["items"]] == ["open"] and (await client.get(f"{API}/admin/disputes")).status_code in (401, 403)


async def test_an_open_dispute_blocks_refunds_until_it_is_resolved(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await send(client, "charge.dispute.created", dispute(pi))
    blocked = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert blocked.status_code == 422 and blocked.json()["code"] == "payment_disputed"
    await send(client, "charge.dispute.closed", dispute(pi, status="won"))
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202


async def test_dispute_transitions_are_explicit_and_a_closed_dispute_never_reopens(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await send(client, "charge.dispute.created", dispute(pi))
    await send(client, "charge.dispute.updated", dispute(pi, status="under_review"))
    assert await one(db_session, "SELECT status FROM disputes") == "under_review"
    await send(client, "charge.dispute.closed", dispute(pi, status="lost"))
    assert await one(db_session, "SELECT status FROM disputes") == "lost"
    kinds = sorted(i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"])
    assert kinds == ["dispute_lost", "dispute_opened"]
    # late or duplicate events cannot move it back, and are ACKNOWLEDGED (200) so the provider stops retrying them
    assert (await send(client, "charge.dispute.updated", dispute(pi, status="needs_response"))).status_code == 200
    assert (await send(client, "charge.dispute.created", dispute(pi))).status_code == 200
    assert await one(db_session, "SELECT status FROM disputes") == "lost"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"   # still no automatic change
    changes = await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'dispute.status_changed'")
    assert changes == 2


async def test_a_dispute_closing_event_that_arrives_first_creates_a_final_record(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert (await send(client, "charge.dispute.closed", dispute(pi, status="won"))).status_code == 200
    assert (await send(client, "charge.dispute.created", dispute(pi))).status_code == 200   # the earlier event, late
    assert (await db_session.execute(text("SELECT status, closed_at IS NOT NULL FROM disputes"))).one() == ("won", True)
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'dispute_opened'") == 0


async def test_the_database_keeps_closed_disputes_final_and_immutable(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    await send(client, "charge.dispute.closed", dispute(pi, status="lost"))
    for sql in ("UPDATE disputes SET status = 'open'", "UPDATE disputes SET amount_cents = 1", "UPDATE disputes SET currency = 'eur'"):
        with pytest.raises(DBAPIError):
            await db_session.execute(text(sql))
        await db_session.rollback()


# ============================================================================================== provider readiness
async def test_every_payment_and_event_records_its_provider(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid(client, admin_headers, category, provider)
    assert await one(db_session, "SELECT provider FROM payments") == "stripe"
    assert await one(db_session, "SELECT DISTINCT provider FROM processed_webhook_events") == "stripe"


def test_the_provider_design_document_covers_the_required_topics():
    doc = pathlib.Path("docs/PAYMENT_PROVIDERS.md").read_text(encoding="utf-8")
    for heading in ("Provider selection", "Credential storage", "Enabling and disabling", "Currency capabilities",
                    "Switching providers", "Secret rotation", "Webhook configuration", "Recommended phase"):
        assert heading in doc, heading


def test_migration_and_model_use_identical_phase28_guard_sql():
    path = next(pathlib.Path("alembic/versions").glob("a2e4c8b6d193_*.py"))
    spec = importlib.util.spec_from_file_location("p28_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = (module.REFUNDS_CUMULATIVE_GUARD_SQL, module.DISPUTES_GUARD_SQL)
    assert tuple(s.strip() for s in migration) == tuple(s.strip() for s in PROVIDER_FUNCTIONS)


def test_live_payments_remain_disabled_by_default():
    assert get_settings().live_payments_enabled is False
