"""
Phase 2.7: order and payment lifecycle integrity. Real PostgreSQL, real HTTP, real job worker code paths,
and a provider fake whose failures (timeouts, uncertainty, racing webhooks) tests control. No live Stripe.
"""

import asyncio
import importlib.util
import json
import pathlib

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.jobs.schedule import enqueue_due, get_schedules, run_scheduler
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
from app.modules.payments.job_types import EXPIRE_ORDER, SWEEP_STALE_ORDERS
from app.modules.payments.models.lifecycle_guard import LIFECYCLE_FUNCTIONS

API = "/api/v1"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


# ---------------------------------------------------------------------------------------------- provider
class LifecycleProvider(PaymentProvider):
    """Stripe-like fake. Modes let a test make the provider time out, be uncertain, or already have charged."""

    def __init__(self):
        self.intents, self.created, self.cancel_calls, self.refund_calls, self.refund_ledger = {}, [], [], [], {}
        self.cancel_mode, self.refund_mode = "canceled", "succeeded"
        self.on_cancel = self.on_create = None
        self.last_pi = None

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        if self.on_create:
            await self.on_create()
        pid = f"pi_lc_{len(self.created) + 1}"
        self.created.append({"amount": amount_cents, "currency": currency})
        self.intents[pid] = {"amount": amount_cents, "currency": currency}
        self.last_pi = pid
        return PaymentIntentResult(pid, f"{pid}_secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        self.cancel_calls.append(provider_payment_id)
        if self.on_cancel:
            await self.on_cancel(provider_payment_id)
        if self.cancel_mode == "timeout":
            raise PaymentProviderTimeoutError()
        if self.cancel_mode == "error":
            raise PaymentProviderError("boom")
        return PaymentIntentResult(provider_payment_id, "secret", self.cancel_mode)

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        self.refund_calls.append({"key": idempotency_key, "amount": amount_cents, "currency": currency})
        if self.refund_mode == "error":
            raise PaymentProviderError("boom")
        if self.refund_mode == "timeout":
            raise PaymentProviderTimeoutError()  # nothing happened at the provider
        entry = self.refund_ledger.get(idempotency_key)
        if entry is None:  # the provider acts ONCE per idempotency key
            status = {"timeout_after_action": "succeeded"}.get(self.refund_mode, self.refund_mode)
            entry = self.refund_ledger[idempotency_key] = {"id": f"re_lc_{len(self.refund_ledger) + 1}", "status": status}
        if self.refund_mode == "timeout_after_action":
            raise PaymentProviderTimeoutError()  # it DID act, but we never heard back
        return RefundResult(entry["id"], amount_cents, entry["status"], currency)

    def verify_and_parse_webhook(self, *, payload, signature_header):
        d = json.loads(payload)
        intent = self.intents.get(d["pi"], {})
        return WebhookEvent(
            d["event_id"], d["type"], d["pi"], d.get("amount", intent.get("amount")), None,
            d.get("currency", intent.get("currency")), d.get("refund_id"), d.get("refund_status"),
        )


@pytest.fixture
def provider(monkeypatch):
    from app.modules.payments.dependencies import payment_deps

    p = LifecycleProvider()
    app.dependency_overrides[get_payment_provider] = lambda: p
    monkeypatch.setattr(payment_deps, "get_payment_provider", lambda: p)  # what the worker's jobs use
    return p


# ---------------------------------------------------------------------------------------------- helpers
@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "LC Cat", "description": None}, headers=admin_headers)).json()["id"]


_n = {"i": 0}


async def make_order(client, admin_headers, category, *, price=1000, stock=10, qty=1):
    _n["i"] += 1
    i = _n["i"]
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"LC {i}", "description": None, "price_cents": price, "sku": f"LC-{i}", "stock": stock,
        "category_id": category, "status": "active"})).json()
    await client.post(f"{API}/auth/register", json={"username": f"buyer_{i}", "email": f"b{i}@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": f"b{i}@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": qty}, headers=h)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": f"lc-{i}"})
    assert r.status_code == 201, r.text
    return h, r.json(), p


async def pay(client, h, order_id):
    r = await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


_e = {"i": 0}


async def hook(client, pi, kind="payment_intent.succeeded", **extra):
    _e["i"] += 1
    body = json.dumps({"event_id": f"evt_lc_{_e['i']}", "type": kind, "pi": pi, **extra})
    return await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=x"})


async def paid_order(client, admin_headers, category, provider, **kw):
    h, o, p = await make_order(client, admin_headers, category, **kw)
    await pay(client, h, o["id"])
    pi = provider.last_pi
    assert (await hook(client, pi)).status_code == 200
    return h, o, p, pi


async def jobs(session_factory):
    import app.jobs  # noqa: F401
    from app.core.jobs.worker import run_one

    ran = 0
    while await run_one(session_factory) and ran < 30:
        ran += 1
    return ran


async def one(db, sql, **params):
    await db.rollback()  # always read fresh committed state
    return (await db.execute(text(sql), params)).scalar_one()


async def stock(client, pid):
    return (await client.get(f"{API}/products/{pid}", headers=client.admin_headers)).json()["stock"]


async def make_stale(db, order_id):
    await db.execute(text("UPDATE orders SET created_at = now() - interval '3 hours' WHERE id = :o"), {"o": order_id})
    await db.commit()


# ============================================================================================== transitions
async def test_the_documented_happy_path_pending_paid_fulfilled_refunded(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    assert await one(db_session, "SELECT status FROM orders WHERE id = :o", o=o["id"]) == "paid"
    f = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)
    assert f.status_code == 200 and f.json()["status"] == "fulfilled"
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    rows = (await db_session.execute(text(
        "SELECT from_status::text, to_status::text FROM order_status_history WHERE order_id = :o ORDER BY id"), {"o": o["id"]})).all()
    assert [tuple(r) for r in rows][-3:] == [("PENDING", "PAID"), ("PAID", "FULFILLED"), ("FULFILLED", "REFUNDED")]


@pytest.mark.parametrize("target", ["paid", "refunded", "cancelled", "pending"])
async def test_admin_cannot_fabricate_payment_refund_or_cancellation_by_status_patch(
    client, admin_headers, category, provider, db_session, target
):
    h, o, p = await make_order(client, admin_headers, category)
    r = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": target}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "status_change_not_allowed", r.text
    assert await one(db_session, "SELECT status FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await stock(client, p["id"]) == 9  # nothing was released or consumed by the refused change


async def test_admin_status_patch_rules_for_fulfilled_and_authorization(client, admin_headers, category, provider):
    h, o, p = await make_order(client, admin_headers, category)
    pending = await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)
    assert pending.status_code in (409, 422)  # an unpaid order cannot be fulfilled
    assert (await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"}, headers=h)).status_code == 403
    assert (await client.patch(f"{API}/orders/{o['id']}/status", json={"status": "fulfilled"})).status_code in (401, 403)
    h2, o2, p2, pi = await paid_order(client, admin_headers, category, provider)
    for _ in range(2):  # a retry changes nothing
        assert (await client.patch(f"{API}/orders/{o2['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)).status_code == 200


@pytest.mark.parametrize("sql", [
    "UPDATE orders SET status = 'paid' WHERE id = :o",                                   # no succeeded payment
    "UPDATE orders SET status = 'refunded' WHERE id = :o",                               # no succeeded refund, wrong step
    "UPDATE orders SET status = 'cancelled' WHERE id = :o",                              # stock not released
    "UPDATE orders SET status = 'fulfilled' WHERE id = :o",                              # pending -> fulfilled
])
async def test_the_database_refuses_illegal_order_status_changes_even_without_the_service(
    client, admin_headers, category, provider, db_session, sql
):
    h, o, p = await make_order(client, admin_headers, category)
    with pytest.raises(DBAPIError):
        await db_session.execute(text(sql), {"o": o["id"]})
    await db_session.rollback()
    assert await one(db_session, "SELECT status FROM orders WHERE id = :o", o=o["id"]) == "pending"


async def test_database_guards_for_final_states_and_one_time_stock_release(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    for sql in ("UPDATE orders SET status = 'cancelled', stock_released_at = now() WHERE id = :o",   # paid -> cancelled
                "UPDATE orders SET status = 'pending' WHERE id = :o"):
        with pytest.raises(DBAPIError):
            await db_session.execute(text(sql), {"o": o["id"]})
        await db_session.rollback()
    h2, o2, p2 = await make_order(client, admin_headers, category)
    await db_session.execute(text("UPDATE orders SET stock_released_at = now() WHERE id = :o"), {"o": o2["id"]})
    await db_session.commit()
    for sql in ("UPDATE orders SET stock_released_at = NULL WHERE id = :o",
                "UPDATE orders SET stock_released_at = now() + interval '1 hour' WHERE id = :o"):
        with pytest.raises(DBAPIError):
            await db_session.execute(text(sql), {"o": o2["id"]})
        await db_session.rollback()


# ============================================================================================== cancellation
async def test_customer_cancels_an_unpaid_order_once_and_stock_comes_back_exactly_once(
    client, admin_headers, category, provider, db_session
):
    h, o, p = await make_order(client, admin_headers, category, qty=3, stock=10)
    await pay(client, h, o["id"])
    assert await stock(client, p["id"]) == 7
    r = await client.post(f"{API}/orders/{o['id']}/cancel", json={"reason": "changed my mind"}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert await stock(client, p["id"]) == 10 and provider.cancel_calls == [provider.last_pi]
    assert await one(db_session, "SELECT status::text FROM payments") == "failed"       # its intent is cancelled
    again = await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)             # duplicate / retry
    assert again.status_code == 200 and await stock(client, p["id"]) == 10               # not restocked twice
    hist = (await db_session.execute(text(
        "SELECT reason, changed_by_user_id IS NOT NULL FROM order_status_history WHERE order_id = :o AND to_status::text = 'CANCELLED'"),
        {"o": o["id"]})).all()
    assert [tuple(x) for x in hist] == [("changed my mind", True)]
    assert "order.cancelled" in [a for (a,) in (await db_session.execute(select(AuditLog.action))).all()]


async def test_only_the_owner_or_an_administrator_can_cancel(client, admin_headers, category, provider):
    h, o, p = await make_order(client, admin_headers, category)
    h2, o2, p2 = await make_order(client, admin_headers, category)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h2)).status_code in (403, 404)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel")).status_code in (401, 403)
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=admin_headers)).status_code == 200


async def test_a_paid_order_cannot_be_cancelled_and_nothing_changes(client, admin_headers, category, provider):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    r = await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "cancellation_not_allowed"
    assert await stock(client, p["id"]) == 9 and provider.cancel_calls == []


async def test_if_the_provider_says_the_payment_already_succeeded_the_order_is_not_cancelled(
    client, admin_headers, category, provider, db_session
):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    provider.cancel_mode = "succeeded"
    r = await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "payment_in_progress"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await stock(client, p["id"]) == 9  # still reserved: the payment will confirm the order


async def test_an_unconfirmable_provider_state_never_cancels_and_is_reconciled_then_recovers(
    client, admin_headers, category, provider, db_session
):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    provider.cancel_mode = "timeout"
    for _ in range(2):  # twice: the problem is recorded once
        r = await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
        assert r.status_code == 409 and r.json()["code"] == "cancellation_unconfirmed"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await stock(client, p["id"]) == 9
    open_ = (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()
    assert [i["kind"] for i in open_["items"]] == ["cancel_uncertain"] and open_["total"] == 1
    provider.cancel_mode = "canceled"  # the provider recovers
    assert (await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)).status_code == 200
    assert await stock(client, p["id"]) == 10


async def test_a_payment_that_wins_the_race_during_cancellation_is_never_cancelled_or_restocked(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])

    async def webhook_wins(_pi):  # a payment succeeds while the provider call is in flight
        async with test_session_factory() as s:
            await s.execute(text("UPDATE payments SET status = 'succeeded'"))
            await s.execute(text("UPDATE orders SET status = 'paid' WHERE id = :o"), {"o": o["id"]})
            await s.commit()

    provider.on_cancel = webhook_wins
    r = await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    assert r.status_code == 409 and r.json()["code"] == "payment_in_progress"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert await stock(client, p["id"]) == 9 and await one(db_session, "SELECT stock_released_at IS NULL FROM orders WHERE id = :o", o=o["id"])


async def test_concurrent_cancellations_restock_exactly_once(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category, qty=2, stock=10)
    await pay(client, h, o["id"])
    results = await asyncio.gather(*[client.post(f"{API}/orders/{o['id']}/cancel", headers=h) for _ in range(4)])
    assert [r.status_code for r in results] == [200] * 4
    assert await stock(client, p["id"]) == 10
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'CANCELLED'", o=o["id"]) == 1


@pytest.mark.parametrize("round_", range(3))
async def test_cancel_racing_a_payment_webhook_always_leaves_a_consistent_order(
    client, admin_headers, category, provider, db_session, round_
):
    h, o, p = await make_order(client, admin_headers, category, qty=2, stock=10)
    await pay(client, h, o["id"])
    pi = provider.last_pi
    cancel, success = await asyncio.gather(
        client.post(f"{API}/orders/{o['id']}/cancel", headers=h), hook(client, pi), return_exceptions=True)
    status = await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"])
    released = not await one(db_session, "SELECT stock_released_at IS NULL FROM orders WHERE id = :o", o=o["id"])
    assert status in ("paid", "cancelled")
    assert (status == "cancelled") == released                      # cancelled <=> stock released, never one without the other
    assert await stock(client, p["id"]) == (10 if released else 8)  # paid keeps stock reserved; cancelled restocked ONCE
    if status == "cancelled":                                       # the money that arrived is recorded for a human
        late = await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'late_payment' AND order_id = :o", o=o["id"])
        paid = await one(db_session, "SELECT status::text FROM payments")
        assert (paid == "succeeded") == (late == 1)


# ============================================================================================== expiry and worker
async def test_stale_unpaid_orders_expire_through_the_scheduled_sweep_safely(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, stale, ps = await make_order(client, admin_headers, category, qty=2)
    h2, fresh, pf = await make_order(client, admin_headers, category)
    h3, paid, pp, _pi = await paid_order(client, admin_headers, category, provider)
    await pay(client, h, stale["id"])
    await make_stale(db_session, stale["id"])
    await make_stale(db_session, paid["id"])  # old, but PAID: must be left alone
    assert SWEEP_STALE_ORDERS in await enqueue_due(test_session_factory, get_settings())   # (other scheduled jobs may be due too)
    await jobs(test_session_factory)
    states = {r[0]: r[1] for r in (await db_session.execute(text("SELECT id::text, status::text FROM orders"))).all()}
    assert (states[stale["id"]], states[fresh["id"]], states[paid["id"]]) == ("cancelled", "pending", "paid")
    assert await stock(client, ps["id"]) == 10 and await stock(client, pf["id"]) == 9 and await stock(client, pp["id"]) == 9
    reason = await one(db_session, "SELECT reason FROM order_status_history WHERE order_id = :o AND to_status::text = 'CANCELLED'", o=stale["id"])
    assert reason == "unpaid order expired" and len(provider.cancel_calls) == 1   # the intent was cancelled first


async def test_expiry_skips_an_order_that_was_paid_after_the_sweep_looked(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    pi = provider.last_pi
    await make_stale(db_session, o["id"])
    await enqueue_due(test_session_factory, get_settings())
    import app.jobs  # noqa: F401
    from app.core.jobs.worker import run_one
    assert await run_one(test_session_factory)            # the sweep queues the expiry job
    assert (await hook(client, pi)).status_code == 200    # ...then the customer's payment lands
    await jobs(test_session_factory)                       # the expiry job now runs
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert provider.cancel_calls == [] and await stock(client, p["id"]) == 9


async def test_expiry_with_an_unreachable_provider_keeps_the_order_and_retries_until_it_can_cancel(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    from app.modules.payments.jobs import expire_order

    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    await make_stale(db_session, o["id"])
    provider.cancel_mode = "timeout"
    for _ in range(2):
        with pytest.raises(RuntimeError):  # the job fails, so the worker retries it with backoff
            await expire_order({"order_id": o["id"]}, test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await stock(client, p["id"]) == 9
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'cancel_uncertain'") == 1
    provider.cancel_mode = "canceled"
    await expire_order({"order_id": o["id"]}, test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"
    assert await stock(client, p["id"]) == 10


async def test_a_crashed_job_is_retried_by_the_queue_and_expiry_still_happens_once(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)
    await make_stale(db_session, o["id"])
    from app.core.jobs.queue import JobQueue

    async with test_session_factory() as s, s.begin():
        await JobQueue(s).enqueue(EXPIRE_ORDER, {"order_id": o["id"]}, dedupe_key="crash-test:1")
        await JobQueue(s).enqueue(EXPIRE_ORDER, {"order_id": o["id"]}, dedupe_key="crash-test:2")  # a duplicate job
    await jobs(test_session_factory)
    assert await stock(client, p["id"]) == 10                       # restocked ONCE although two jobs ran
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'CANCELLED'", o=o["id"]) == 1


async def test_the_scheduler_enqueues_the_sweep_once_per_interval_and_can_be_disabled(
    client, test_session_factory, db_session, monkeypatch
):
    assert SWEEP_STALE_ORDERS in [s.job_type for s in get_schedules()]
    settings = get_settings()
    monkeypatch.setattr(settings, "order_expiry_sweep_interval_seconds", 3600)
    first = await asyncio.gather(*[enqueue_due(test_session_factory, settings) for _ in range(5)])  # five workers at once
    assert sum(x.count(SWEEP_STALE_ORDERS) for x in first) == 1          # once, however many workers tick at once
    assert await one(db_session, "SELECT count(*) FROM background_jobs WHERE job_type = :t", t=SWEEP_STALE_ORDERS) == 1
    # every scheduled job has the same once-per-interval guarantee (the deferred-event recovery job too)
    assert sum(x.count("reprocess_deferred_events") for x in first) == 1
    monkeypatch.setattr(settings, "order_expiry_sweep_interval_seconds", 0)
    assert SWEEP_STALE_ORDERS not in await enqueue_due(test_session_factory, settings)  # 0 = disabled
    monkeypatch.setattr(settings, "webhook_recovery_interval_seconds", 0)
    assert await enqueue_due(test_session_factory, settings) == []                      # both disabled: nothing is enqueued


async def test_the_scheduler_loop_runs_and_stops_cleanly(client, test_session_factory, db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "order_expiry_sweep_interval_seconds", 2)
    stop = asyncio.Event()
    task = asyncio.create_task(run_scheduler(stop, test_session_factory, settings))
    await asyncio.sleep(1.2)
    stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert await one(db_session, "SELECT count(*) FROM background_jobs WHERE job_type = :t", t=SWEEP_STALE_ORDERS) >= 1


async def test_lifecycle_health_reports_what_is_stuck_and_whether_the_sweep_is_running(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    assert (await client.get(f"{API}/admin/lifecycle/health")).status_code in (401, 403)
    h, o, p = await make_order(client, admin_headers, category)
    await make_stale(db_session, o["id"])
    body = (await client.get(f"{API}/admin/lifecycle/health", headers=admin_headers)).json()
    assert body["stale_pending_orders"] == 1 and body["oldest_pending_age_seconds"] >= 3 * 3600 - 60
    assert body["sweep_overdue"] is True and body["last_sweep_succeeded_at"] is None  # stale orders but no sweep has run
    await enqueue_due(test_session_factory, get_settings())
    await jobs(test_session_factory)
    body = (await client.get(f"{API}/admin/lifecycle/health", headers=admin_headers)).json()
    assert body["stale_pending_orders"] == 0 and body["sweep_overdue"] is False and body["last_sweep_succeeded_at"]
    assert body["sweep_interval_seconds"] == get_settings().order_expiry_sweep_interval_seconds
    assert set(body["active_refunds"]) == {"requested", "pending", "uncertain"}


def test_the_worker_is_a_deployed_service_in_both_compose_files():
    for name in ("docker-compose.yml", "docker-compose.prod.yml"):
        if not pathlib.Path(name).exists():
            pytest.skip(f"{name} is not in this environment (the application image does not contain it); "
                        "check it on the host: Select-String -Path docker-compose*.yml -Pattern run_worker")
        text_ = pathlib.Path(name).read_text(encoding="utf-8")
        assert "worker:" in text_ and "python -m scripts.run_worker" in text_, name


# ============================================================================================== late / unmatched payments
async def test_a_late_payment_is_recorded_once_for_reconciliation_and_never_auto_refunded_or_fulfilled(
    client, admin_headers, category, provider, db_session
):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    pi = provider.last_pi
    provider.on_cancel = None
    # the order is cancelled while the customer's payment is still in flight at the provider
    await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    r1 = await hook(client, pi, event_id="evt_late_fixed")
    r2 = await hook(client, pi, event_id="evt_late_fixed")            # a redelivered event
    assert r1.status_code == r2.status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"
    assert await one(db_session, "SELECT status::text FROM payments") == "succeeded"   # the money did arrive
    items = (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]
    assert [i["kind"] for i in items] == ["late_payment"] and items[0]["order_id"] == o["id"]
    assert provider.refund_calls == [] and await one(db_session, "SELECT count(*) FROM refunds") == 0
    assert await one(db_session, "SELECT count(*) FROM background_jobs WHERE job_type = 'execute_refund'") == 0


async def test_an_administrator_can_refund_a_late_payment_explicitly_and_the_order_stays_cancelled(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    pi = provider.last_pi
    await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    await hook(client, pi)
    rec = (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"][0]
    r = await client.post(f"{API}/admin/reconciliations/{rec['id']}/refund", headers=admin_headers)
    assert r.status_code == 202 and r.json()["status"] == "requested"
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"
    done = (await client.get(f"{API}/admin/reconciliations", params={"status_filter": "resolved"}, headers=admin_headers)).json()["items"]
    assert done[0]["resolution"] == "refund_requested"
    again = await client.post(f"{API}/admin/reconciliations/{rec['id']}/refund", headers=admin_headers)
    assert again.status_code == 422 and again.json()["code"] == "reconciliation_not_open"
    assert len(provider.refund_ledger) == 1


async def test_reconciliation_records_need_an_administrator_and_a_written_resolution(client, admin_headers, category, provider):
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    pi = provider.last_pi
    await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    await hook(client, pi)
    rec = (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"][0]
    assert (await client.get(f"{API}/admin/reconciliations", headers=h)).status_code == 403
    assert (await client.post(f"{API}/admin/reconciliations/{rec['id']}/resolve", headers=h,
                              json={"resolution": "no_action_needed", "note": "x" * 5})).status_code == 403
    bad = await client.post(f"{API}/admin/reconciliations/{rec['id']}/resolve", headers=admin_headers,
                            json={"resolution": "no_action_needed", "note": ""})
    assert bad.status_code == 422
    ok = await client.post(f"{API}/admin/reconciliations/{rec['id']}/resolve", headers=admin_headers,
                           json={"resolution": "handled_externally", "note": "Refunded in the Stripe dashboard"})
    assert ok.status_code == 200 and ok.json()["status"] == "resolved" and "dashboard" in ok.json()["resolution_note"]


async def test_a_payment_for_an_unknown_intent_is_recorded_not_applied(client, admin_headers, provider, db_session):
    assert (await hook(client, "pi_nobody", amount=500, currency="usd")).status_code == 200
    items = (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]
    assert [i["kind"] for i in items] == ["unmatched_payment"]


# ============================================================================================== refunds
async def test_a_refund_is_requested_first_and_only_a_provider_confirmed_result_changes_the_order(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    first = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    second = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)   # retry / double click
    assert first.status_code == second.status_code == 202 and first.json()["id"] == second.json()["id"]
    assert first.json()["status"] == "requested" and provider.refund_calls == []
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"   # not refunded yet
    await jobs(test_session_factory)
    await jobs(test_session_factory)
    assert len(provider.refund_calls) == 1 and len(provider.refund_ledger) == 1                       # sent once
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    assert await one(db_session, "SELECT count(*) FROM refunds") == 1


async def test_the_database_allows_only_one_active_refund_per_payment_and_final_states_are_final(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    await jobs(test_session_factory)
    pid = await one(db_session, "SELECT payment_id::text FROM refunds")
    with pytest.raises(DBAPIError):  # a second active refund for the same payment
        await db_session.execute(text(
            "INSERT INTO refunds (id, payment_id, order_id, amount_cents, currency, idempotency_key) "
            "VALUES (gen_random_uuid(), :p, :o, 1, 'usd', 'dup-key')"), {"p": pid, "o": o["id"]})
    await db_session.rollback()
    for sql in ("UPDATE refunds SET status = 'requested'", "UPDATE refunds SET amount_cents = 1", "UPDATE refunds SET currency = 'eur'"):
        with pytest.raises(DBAPIError):
            await db_session.execute(text(sql))
        await db_session.rollback()


async def test_an_order_cannot_become_refunded_without_a_succeeded_refund_row(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    with pytest.raises(DBAPIError):
        await db_session.execute(text("UPDATE orders SET status = 'refunded' WHERE id = :o"), {"o": o["id"]})
    await db_session.rollback()


@pytest.mark.parametrize("make", ["pending", "refunded_already"])
async def test_refunds_are_refused_for_orders_that_are_not_refundable(client, admin_headers, category, provider, test_session_factory, make):
    if make == "pending":
        h, o, p = await make_order(client, admin_headers, category)
    else:
        h, o, p, pi = await paid_order(client, admin_headers, category, provider)
        await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
        await jobs(test_session_factory)
    r = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert r.status_code == 422, r.text


async def test_refund_requests_need_an_administrator_and_respect_the_live_payments_guard(
    client, admin_headers, category, provider, monkeypatch
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=h)).status_code == 403
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_live_not_real")
    r = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert r.status_code == 503 and r.json()["code"] == "live_payments_disabled"


async def test_a_refund_timeout_is_retried_with_the_same_key_and_never_refunds_twice(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    from app.modules.payments.jobs import execute_refund

    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    rid = (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).json()["id"]
    provider.refund_mode = "timeout_after_action"   # the provider refunds, but we never hear back
    with pytest.raises(PaymentProviderError):
        await execute_refund({"refund_id": rid}, test_session_factory)
    assert await one(db_session, "SELECT status FROM refunds") == "requested"             # NOT assumed failed
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    provider.refund_mode = "succeeded"
    await execute_refund({"refund_id": rid}, test_session_factory)                         # the retry
    assert len({c["key"] for c in provider.refund_calls}) == 1 and len(provider.refund_ledger) == 1
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await one(db_session, "SELECT attempts FROM refunds") == 2


async def test_when_retries_run_out_the_refund_is_uncertain_not_failed_and_a_later_provider_event_resolves_it(
    client, admin_headers, category, provider, db_session, test_session_factory, monkeypatch
):
    from app.modules.payments.jobs import execute_refund

    monkeypatch.setattr(get_settings(), "refund_max_attempts", 2)
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    rid = (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).json()["id"]
    provider.refund_mode = "timeout"
    with pytest.raises(PaymentProviderError):
        await execute_refund({"refund_id": rid}, test_session_factory)   # attempt 1: retry
    await execute_refund({"refund_id": rid}, test_session_factory)       # attempt 2: gives up, does not raise
    assert await one(db_session, "SELECT status FROM refunds") == "uncertain"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert kinds == ["refund_uncertain"]
    calls = len(provider.refund_calls)
    await execute_refund({"refund_id": rid}, test_session_factory)       # no further provider calls once uncertain
    assert len(provider.refund_calls) == calls
    # later the provider tells us the truth through its webhook
    r = await hook(client, pi, kind="refund.updated", refund_id="re_real_1", refund_status="succeeded", amount=o["total_cents"], currency="usd")
    assert r.status_code == 200
    assert await one(db_session, "SELECT status FROM refunds") == "succeeded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"


async def test_pending_then_confirmed_by_webhook_and_failed_refunds_can_be_retried(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    provider.refund_mode = "pending"
    await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status FROM refunds") == "pending"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    rid = await one(db_session, "SELECT provider_refund_id FROM refunds")
    for _ in range(2):  # the confirmation, redelivered
        assert (await hook(client, pi, kind="refund.updated", refund_id=rid, refund_status="succeeded",
                           amount=o["total_cents"], currency="usd")).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    # a different order: the provider reports the refund FAILED, so the order stays paid and a new refund can be requested
    h2, o2, p2, pi2 = await paid_order(client, admin_headers, category, provider)
    provider.refund_mode = "failed"
    await client.post(f"{API}/payments/{o2['id']}/refund", headers=admin_headers)
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o2["id"]) == "paid"
    provider.refund_mode = "succeeded"
    again = await client.post(f"{API}/payments/{o2['id']}/refund", headers=admin_headers)
    assert again.status_code == 202 and again.json()["status"] == "requested"
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o2["id"]) == "refunded"
    keys = [r[0] for r in (await db_session.execute(text("SELECT idempotency_key FROM refunds WHERE order_id = :o"), {"o": o2["id"]})).all()]
    assert len(set(keys)) == 2  # the retry used a NEW key: a failed refund is not replayed


async def test_a_refund_made_in_the_provider_dashboard_is_recorded_but_never_changes_the_order(client, admin_headers, category, provider, db_session):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    assert (await hook(client, pi, kind="refund.created", refund_id="re_dash_1", refund_status="succeeded",
                       amount=o["total_cents"], currency="usd")).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"   # a business decision
    assert await one(db_session, "SELECT origin FROM refunds") == "provider"                              # but the money fact is recorded
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert kinds == ["external_refund"]


async def test_a_refund_amount_that_differs_from_the_request_is_flagged_even_when_confirmed(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    rid = None
    provider.refund_mode = "pending"
    await jobs(test_session_factory)
    rid = await one(db_session, "SELECT provider_refund_id FROM refunds")
    await hook(client, pi, kind="refund.updated", refund_id=rid, refund_status="succeeded", amount=o["total_cents"] - 1, currency="usd")
    kinds = [i["kind"] for i in (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()["items"]]
    assert "refund_mismatch" in kinds


# ============================================================================================== failures and rollback
async def test_a_provider_timeout_while_creating_a_payment_leaves_no_payment_and_the_order_intact(
    client, admin_headers, category, provider, db_session
):
    h, o, p = await make_order(client, admin_headers, category)

    async def boom():
        raise PaymentProviderTimeoutError()

    provider.on_create = boom
    r = await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    assert r.status_code in (502, 503, 504)
    assert await one(db_session, "SELECT count(*) FROM payments") == 0
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await stock(client, p["id"]) == 9
    provider.on_create = None
    assert (await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)).status_code == 201  # simply retry


async def test_an_intent_created_while_the_order_is_being_cancelled_is_cancelled_too(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p = await make_order(client, admin_headers, category)

    async def cancel_during_create():  # the order is cancelled while the provider is creating the intent
        async with test_session_factory() as s:
            await s.execute(text("UPDATE orders SET stock_released_at = now() WHERE id = :o"), {"o": o["id"]})
            await s.execute(text("UPDATE orders SET status = 'cancelled' WHERE id = :o"), {"o": o["id"]})
            await s.commit()

    provider.on_create = cancel_during_create
    r = await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    assert r.status_code == 422
    assert await one(db_session, "SELECT count(*) FROM payments") == 0          # no payment row for a cancelled order
    assert provider.cancel_calls == [provider.last_pi]                           # the orphaned intent was cancelled


async def test_stock_and_payments_stay_consistent_when_a_request_fails_midway(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category, qty=3, stock=5)
    provider.cancel_mode = "error"
    await pay(client, h, o["id"])
    r = await client.post(f"{API}/orders/{o['id']}/cancel", headers=h)
    assert r.status_code == 409
    assert await stock(client, p["id"]) == 2 and await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await one(db_session, "SELECT status::text FROM payments") == "pending"


def test_migration_and_model_use_identical_lifecycle_guard_sql():
    path = next(pathlib.Path("alembic/versions").glob("f9c3b6e1d752_*.py"))
    spec = importlib.util.spec_from_file_location("lifecycle_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = (module.ORDERS_LIFECYCLE_GUARD_SQL, module.REFUNDS_GUARD_SQL)
    assert tuple(s.strip() for s in migration) == tuple(s.strip() for s in LIFECYCLE_FUNCTIONS)
