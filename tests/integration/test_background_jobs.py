"""
The background job queue, against a real Postgres.

Covers the queue itself (atomic enqueue, dedupe, retries with backoff, crash
recovery, no job ever runs twice at once) and the first real job: the order
confirmation email queued by the Stripe webhook, including the roadmap's
acceptance test -- a slow downstream (the email provider) does not slow the
webhook response.
"""

import asyncio
import json
import time
import uuid

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.jobs import JobQueue, registry
from app.core.jobs.worker import (
    claim_next,
    complete_job,
    retry_delay_seconds,
    run_one,
    run_worker,
)
from app.core.payments.interface import PaymentIntentResult, PaymentProvider, WebhookEvent
from app.modules.auth.models.user import User
from app.modules.categories.models.category import Category
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.payments.dependencies.payment_deps import get_payment_provider
from app.modules.payments.jobs import SEND_ORDER_CONFIRMATION
from app.modules.payments.models.payment import Payment
from app.modules.products.models.product import Product
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole

# --- helpers --------------------------------------------------------------------------


def fast_config(**overrides):
    """Real settings, with retry delays short enough for a test."""
    values = {
        "jobs_poll_interval_seconds": 0.05,
        "jobs_retry_base_seconds": 0,
        "jobs_retry_max_seconds": 0,
        "jobs_job_timeout_seconds": 5,
        "jobs_lock_seconds": 30,
    }
    values.update(overrides)
    return get_settings().model_copy(update=values)


async def job_row(db_session, job_id):
    result = await db_session.execute(
        text(
            "SELECT status, attempts, max_attempts, last_error, finished_at, "
            "       run_at > now() AS in_future, locked_until "
            "FROM background_jobs WHERE id = :id"
        ),
        {"id": job_id},
    )
    return result.one()


async def enqueue(db_session, job_type="test_job", payload=None, **kwargs):
    job_id = await JobQueue(db_session).enqueue(job_type, payload, **kwargs)
    await db_session.commit()
    return job_id


@pytest.fixture
def register_handler(monkeypatch):
    def register(job_type, func):
        monkeypatch.setitem(registry._handlers, job_type, func)

    return register


# --- enqueue ---------------------------------------------------------------------------


async def test_enqueue_saves_a_pending_job(db_session):
    job_id = await enqueue(db_session, payload={"a": 1})

    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("pending", 0)
    assert row.max_attempts == get_settings().jobs_default_max_attempts
    assert row.in_future is False


async def test_enqueue_with_delay_is_not_due_yet(db_session, test_session_factory, register_handler):
    ran = []

    async def handler(payload, session_factory):
        ran.append(payload)

    register_handler("test_job", handler)
    job_id = await enqueue(db_session, delay_seconds=60)

    assert (await job_row(db_session, job_id)).in_future is True
    assert await run_one(test_session_factory, fast_config()) is False
    assert ran == []


async def test_enqueue_is_part_of_the_callers_transaction(db_session):
    """Rolled back with the business change: no job without the change that needed it."""
    await JobQueue(db_session).enqueue("test_job", {"a": 1})
    await db_session.rollback()

    count = (await db_session.execute(text("SELECT count(*) FROM background_jobs"))).scalar_one()
    assert count == 0


async def test_dedupe_key_prevents_a_second_job(db_session):
    first = await enqueue(db_session, dedupe_key="thing:1")
    second = await enqueue(db_session, dedupe_key="thing:1")
    other = await enqueue(db_session, dedupe_key="thing:2")

    assert first is not None and other is not None
    assert second is None
    count = (await db_session.execute(text("SELECT count(*) FROM background_jobs"))).scalar_one()
    assert count == 2


async def test_jobs_without_a_dedupe_key_are_never_deduplicated(db_session):
    await enqueue(db_session)
    await enqueue(db_session)

    count = (await db_session.execute(text("SELECT count(*) FROM background_jobs"))).scalar_one()
    assert count == 2


# --- running jobs -------------------------------------------------------------------------


async def test_a_job_runs_once_with_its_payload_and_is_marked_succeeded(
    db_session, test_session_factory, register_handler
):
    seen = []

    async def handler(payload, session_factory):
        seen.append(payload)

    register_handler("test_job", handler)
    job_id = await enqueue(db_session, payload={"order_id": "abc"})

    assert await run_one(test_session_factory, fast_config()) is True
    assert await run_one(test_session_factory, fast_config()) is False  # nothing left

    assert seen == [{"order_id": "abc"}]
    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("succeeded", 1)
    assert row.finished_at is not None and row.last_error is None


async def test_a_failing_job_is_retried_later_and_records_the_error(
    db_session, test_session_factory, register_handler
):
    async def handler(payload, session_factory):
        raise RuntimeError("provider is down")

    register_handler("test_job", handler)
    job_id = await enqueue(db_session)
    config = fast_config(jobs_retry_base_seconds=600, jobs_retry_max_seconds=600)

    assert await run_one(test_session_factory, config) is True

    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("pending", 1)
    assert "RuntimeError: provider is down" in row.last_error
    assert row.in_future is True  # backoff: not due again for ~10 minutes
    assert await run_one(test_session_factory, config) is False


async def test_a_job_that_keeps_failing_ends_up_failed_after_max_attempts(
    db_session, test_session_factory, register_handler
):
    calls = 0

    async def handler(payload, session_factory):
        nonlocal calls
        calls += 1
        raise RuntimeError("always broken")

    register_handler("test_job", handler)
    job_id = await enqueue(db_session, max_attempts=3)

    while await run_one(test_session_factory, fast_config()):
        pass

    assert calls == 3
    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.finished_at is not None
    assert "always broken" in row.last_error


async def test_a_job_that_fails_then_succeeds(db_session, test_session_factory, register_handler):
    attempts = []

    async def handler(payload, session_factory):
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("flaky")

    register_handler("test_job", handler)
    job_id = await enqueue(db_session)

    while await run_one(test_session_factory, fast_config()):
        pass

    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("succeeded", 3)
    assert row.last_error is None


async def test_a_job_that_takes_too_long_counts_as_a_failure(
    db_session, test_session_factory, register_handler
):
    async def handler(payload, session_factory):
        await asyncio.sleep(10)

    register_handler("test_job", handler)
    job_id = await enqueue(db_session)

    started = time.monotonic()
    await run_one(test_session_factory, fast_config(jobs_job_timeout_seconds=0.2))

    assert time.monotonic() - started < 2
    row = await job_row(db_session, job_id)
    assert row.status == "pending"
    assert "TimeoutError" in row.last_error


async def test_a_job_type_nobody_handles_is_retried_then_failed(db_session, test_session_factory):
    job_id = await enqueue(db_session, job_type="nobody_handles_this", max_attempts=2)

    while await run_one(test_session_factory, fast_config()):
        pass

    row = await job_row(db_session, job_id)
    assert row.status == "failed"
    assert "no handler registered" in row.last_error


def test_the_lock_must_outlast_the_job_timeout():
    """Otherwise a second worker could take a job that is still running."""
    from pydantic import ValidationError

    from app.core.config import Settings

    values = {**get_settings().model_dump(), "jobs_lock_seconds": 30, "jobs_job_timeout_seconds": 60}
    with pytest.raises(ValidationError, match="jobs_lock_seconds"):
        Settings.model_validate(values)


def test_retry_delay_doubles_and_is_capped():
    delays = [retry_delay_seconds(n, base=30, cap=3600) for n in range(1, 10)]
    assert delays[:4] == [30, 60, 120, 240]
    assert max(delays) == 3600


async def test_jobs_run_oldest_due_first(db_session, test_session_factory, register_handler):
    order = []

    async def handler(payload, session_factory):
        order.append(payload["n"])

    register_handler("test_job", handler)
    await enqueue(db_session, payload={"n": 1})
    await enqueue(db_session, payload={"n": 2})
    await enqueue(db_session, payload={"n": 3})

    while await run_one(test_session_factory, fast_config()):
        pass

    assert order == [1, 2, 3]


# --- crashes -------------------------------------------------------------------------------


async def test_a_job_whose_worker_died_is_taken_over_after_its_lock_expires(
    db_session, test_session_factory, register_handler
):
    ran = []

    async def handler(payload, session_factory):
        ran.append(1)

    register_handler("test_job", handler)
    job_id = await enqueue(db_session)

    # A worker claims it and then "dies": the job is left running, locked.
    claimed = await claim_next(test_session_factory, fast_config(jobs_lock_seconds=300))
    assert claimed is not None and claimed.attempts == 1
    assert await run_one(test_session_factory, fast_config()) is False  # still locked: hands off

    await db_session.execute(
        text("UPDATE background_jobs SET locked_until = now() - interval '1 second'")
    )
    await db_session.commit()

    assert await run_one(test_session_factory, fast_config()) is True  # lock expired: taken over
    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("succeeded", 2)
    assert ran == [1]


async def test_a_job_whose_worker_keeps_dying_is_eventually_marked_failed(
    db_session, test_session_factory
):
    job_id = await enqueue(db_session, max_attempts=2)

    for _ in range(2):  # two workers claim it and die
        assert await claim_next(test_session_factory, fast_config()) is not None
        await db_session.execute(
            text("UPDATE background_jobs SET locked_until = now() - interval '1 second'")
        )
        await db_session.commit()

    assert await run_one(test_session_factory, fast_config()) is False  # no third try

    row = await job_row(db_session, job_id)
    assert row.status == "failed"
    assert "out of attempts" in row.last_error


async def test_a_late_result_from_a_worker_that_lost_its_lock_is_ignored(
    db_session, test_session_factory
):
    """Worker A is slow, its lock expires, worker B takes the job. A's result must not count."""
    job_id = await enqueue(db_session)
    config = fast_config()

    worker_a = await claim_next(test_session_factory, config)
    await db_session.execute(
        text("UPDATE background_jobs SET locked_until = now() - interval '1 second'")
    )
    await db_session.commit()
    worker_b = await claim_next(test_session_factory, config)
    assert (worker_a.attempts, worker_b.attempts) == (1, 2)

    assert await complete_job(test_session_factory, worker_a) is False
    row = await job_row(db_session, job_id)
    assert (row.status, row.attempts) == ("running", 2)

    assert await complete_job(test_session_factory, worker_b) is True
    assert (await job_row(db_session, job_id)).status == "succeeded"


# --- concurrency ------------------------------------------------------------------------------


async def test_many_workers_never_run_the_same_job_twice(
    db_session, test_session_factory, register_handler
):
    executed = []

    async def handler(payload, session_factory):
        await asyncio.sleep(0.01)
        executed.append(payload["n"])

    register_handler("test_job", handler)
    total = 60
    for n in range(total):
        await JobQueue(db_session).enqueue("test_job", {"n": n})
    await db_session.commit()

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker(stop, session_factory=test_session_factory, config=fast_config(), concurrency=8)
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        done = (
            await db_session.execute(
                text("SELECT count(*) FROM background_jobs WHERE status = 'succeeded'")
            )
        ).scalar_one()
        if done == total:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)

    assert sorted(executed) == list(range(total))  # each exactly once
    attempts = (
        await db_session.execute(text("SELECT DISTINCT attempts FROM background_jobs"))
    ).scalars().all()
    assert attempts == [1]


async def test_the_worker_stops_promptly_and_finishes_the_job_it_is_running(
    db_session, test_session_factory, register_handler
):
    started, finished = asyncio.Event(), []

    async def handler(payload, session_factory):
        started.set()
        await asyncio.sleep(0.3)
        finished.append(1)

    register_handler("test_job", handler)
    job_id = await enqueue(db_session)

    stop = asyncio.Event()
    worker = asyncio.create_task(
        run_worker(stop, session_factory=test_session_factory, config=fast_config(), concurrency=1)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(worker, timeout=5)

    assert finished == [1]
    assert (await job_row(db_session, job_id)).status == "succeeded"


async def test_the_api_can_run_the_worker_inside_itself(
    db_session, test_session_factory, register_handler, monkeypatch
):
    import app.main as main

    ran = asyncio.Event()

    async def handler(payload, session_factory):
        ran.set()

    register_handler("test_job", handler)
    monkeypatch.setattr(main.settings, "jobs_embedded_worker", True)
    monkeypatch.setattr(main, "async_session_factory", test_session_factory)
    job_id = await enqueue(db_session)

    async with main.app.router.lifespan_context(main.app):
        await asyncio.wait_for(ran.wait(), timeout=5)

    # The worker was stopped by the lifespan exit, after finishing its job.
    deadline = time.monotonic() + 5
    while (await job_row(db_session, job_id)).status != "succeeded":
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)


# --- the webhook: first real job, and the roadmap's acceptance test ------------------------------


class FakeProvider(PaymentProvider):
    """Treats the request body as JSON: {"event_id": ..., "provider_payment_id": ...}."""

    async def create_payment_intent(self, **kwargs):
        raise NotImplementedError

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        return PaymentIntentResult(provider_payment_id, "secret", "canceled")

    async def retrieve_payment_intent(self, **kwargs):
        raise NotImplementedError

    async def create_refund(self, **kwargs):
        raise NotImplementedError

    def verify_and_parse_webhook(self, *, payload, signature_header):
        data = json.loads(payload)
        return WebhookEvent(
            event_id=data["event_id"],
            event_type="payment_intent.succeeded",
            provider_payment_id=data["provider_payment_id"],
            amount=data.get("amount", 5000),
            currency=data.get("currency", "usd"),
        )


class RecordingSender:
    def __init__(self, delay: float = 0) -> None:
        self.delay = delay
        self.sent: list[dict] = []

    async def send(self, *, to, subject, body):
        await asyncio.sleep(self.delay)
        self.sent.append({"to": to, "subject": subject, "body": body})


async def _pending_paid_order(db_session, *, guest: bool = False):
    unique = uuid.uuid4().hex[:8]
    category = Category(name=f"Jobs {unique}", slug=f"jobs-{unique}")
    db_session.add(category)
    await db_session.flush()
    product = Product(
        name="Gadget", slug=f"gadget-{unique}", price_cents=2500, sku=f"G-{unique}", stock=5,
        category_id=category.id, status=ProductStatus.ACTIVE,
    )  # fmt: skip
    db_session.add(product)
    await db_session.flush()

    owner = {}
    if guest:
        owner = {"guest_token": f"tok-{unique}", "guest_email": f"guest-{unique}@example.com"}
        recipient = owner["guest_email"]
    else:
        user = User(
            id=uuid.uuid4(), username=f"buyer_{unique}", email=f"buyer_{unique}@example.com",
            password_hash="irrelevant", role=UserRole.USER, is_active=True,
        )  # fmt: skip
        db_session.add(user)
        await db_session.flush()
        owner = {"user_id": user.id}
        recipient = user.email

    order = Order(
        id=uuid.uuid4(), idempotency_key=f"k-{unique}", status=OrderStatus.PENDING,
        subtotal_cents=5000, shipping_cents=0, tax_cents=0, total_cents=5000, **owner,
    )  # fmt: skip
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id, product_id=product.id, product_name_snapshot="Gadget",
            price_cents_snapshot=2500, quantity=2,
        )
    )  # fmt: skip
    provider_payment_id = f"pi_{unique}"
    db_session.add(
        Payment(
            order_id=order.id, provider_payment_id=provider_payment_id,
            amount_cents=5000, currency="usd",
        )
    )  # fmt: skip
    await db_session.commit()
    return order.id, provider_payment_id, recipient


async def _post_webhook(client, event_id, provider_payment_id):
    return await client.post(
        "/api/v1/payments/webhook",
        content=json.dumps({"event_id": event_id, "provider_payment_id": provider_payment_id}),
        headers={"Stripe-Signature": "t=1,v1=fake", "content-type": "application/json"},
    )


@pytest.fixture
def fake_provider():
    from app.main import app

    app.dependency_overrides[get_payment_provider] = lambda: FakeProvider()
    yield
    app.dependency_overrides.pop(get_payment_provider, None)


async def test_slow_email_provider_does_not_slow_the_webhook(
    client, db_session, test_session_factory, fake_provider, monkeypatch
):
    """
    The roadmap's acceptance test. The email provider takes a full second; the
    webhook must answer without waiting for it, and the email must still be
    sent -- by the worker.
    """
    sender = RecordingSender(delay=1.0)
    monkeypatch.setattr("app.modules.payments.jobs.get_email_sender", lambda: sender)
    order_id, payment_id, recipient = await _pending_paid_order(db_session)

    started = time.perf_counter()
    response = await _post_webhook(client, "evt_1", payment_id)
    webhook_seconds = time.perf_counter() - started

    assert response.status_code == 200
    assert webhook_seconds < 0.5  # an inline send would take over 1 second
    assert sender.sent == []  # nothing was sent inside the request
    status = (
        await db_session.execute(text("SELECT status FROM orders WHERE id = :id"), {"id": order_id})
    ).scalar_one()
    assert status == "paid"
    jobs = (
        await db_session.execute(text("SELECT job_type, status, payload FROM background_jobs"))
    ).all()
    assert [(j.job_type, j.status, j.payload) for j in jobs] == [
        (SEND_ORDER_CONFIRMATION, "pending", {"order_id": str(order_id)})
    ]

    # The worker does the slow part, outside any request.
    worker_started = time.perf_counter()
    assert await run_one(test_session_factory, fast_config()) is True
    assert time.perf_counter() - worker_started >= 1.0

    assert len(sender.sent) == 1
    assert sender.sent[0]["to"] == recipient
    assert str(order_id)[:8] in sender.sent[0]["subject"]
    assert "2 x Gadget" in sender.sent[0]["body"] and "50.00" in sender.sent[0]["body"]


async def test_guest_orders_are_emailed_at_the_guest_address(
    client, db_session, test_session_factory, fake_provider, monkeypatch
):
    sender = RecordingSender()
    monkeypatch.setattr("app.modules.payments.jobs.get_email_sender", lambda: sender)
    _, payment_id, recipient = await _pending_paid_order(db_session, guest=True)

    await _post_webhook(client, "evt_guest", payment_id)
    await run_one(test_session_factory, fast_config())

    assert [m["to"] for m in sender.sent] == [recipient]


async def test_a_duplicate_webhook_delivery_queues_one_email(
    client, db_session, test_session_factory, fake_provider, monkeypatch
):
    sender = RecordingSender()
    monkeypatch.setattr("app.modules.payments.jobs.get_email_sender", lambda: sender)
    _, payment_id, _ = await _pending_paid_order(db_session)

    first = await _post_webhook(client, "evt_dup", payment_id)  # Stripe delivers twice
    second = await _post_webhook(client, "evt_dup", payment_id)
    third = await _post_webhook(client, "evt_other", payment_id)  # new event, already paid

    assert first.status_code == second.status_code == third.status_code == 200
    count = (await db_session.execute(text("SELECT count(*) FROM background_jobs"))).scalar_one()
    assert count == 1
    while await run_one(test_session_factory, fast_config()):
        pass
    assert len(sender.sent) == 1


async def test_a_failing_email_provider_is_retried_without_touching_the_order(
    client, db_session, test_session_factory, fake_provider, monkeypatch
):
    class FlakySender:
        calls = 0

        async def send(self, *, to, subject, body):
            FlakySender.calls += 1
            if FlakySender.calls == 1:
                raise ConnectionError("smtp down")

    monkeypatch.setattr("app.modules.payments.jobs.get_email_sender", lambda: FlakySender())
    order_id, payment_id, _ = await _pending_paid_order(db_session)

    response = await _post_webhook(client, "evt_flaky", payment_id)
    assert response.status_code == 200  # the webhook never depended on the email

    while await run_one(test_session_factory, fast_config()):
        pass

    assert FlakySender.calls == 2
    row = (await db_session.execute(text("SELECT status, attempts FROM background_jobs"))).one()
    assert (row.status, row.attempts) == ("succeeded", 2)
    order_status = (
        await db_session.execute(text("SELECT status FROM orders WHERE id = :id"), {"id": order_id})
    ).scalar_one()
    assert order_status == "paid"
