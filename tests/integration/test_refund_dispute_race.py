# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""H2: a refund must not be SUBMITTED while a dispute is open; overlaps that cannot be prevented are recorded for a human."""

import asyncio
import contextlib
import uuid

from sqlalchemy import text
from test_order_payment_lifecycle import (  # noqa: F401  (fixtures + helpers shared with the lifecycle suite)
    API,
    category,
    jobs,
    one,
    paid_order,
    provider,
)

from app.core.payments.exceptions import PaymentProviderError
from app.core.payments.interface import WebhookEvent
from app.core.rate_limit import limiter
from app.modules.payments.jobs import _build_lifecycle, execute_refund


async def open_dispute(db, order_id, status="open", dispute_id="dp_h2_1"):
    payment_id = await one(db, "SELECT id FROM payments WHERE order_id = :o", o=order_id)
    await db.execute(text(
        "INSERT INTO disputes (id, provider, provider_dispute_id, payment_id, order_id, amount_cents, currency, status) "
        "VALUES (gen_random_uuid(), 'stripe', :d, :p, :o, 1000, 'usd', :s)"), {"d": dispute_id, "p": payment_id, "o": order_id, "s": status})
    await db.commit()


async def kinds(db):
    await db.rollback()
    return sorted(r[0] for r in (await db.execute(text("SELECT kind FROM payment_reconciliations"))).all())


async def test_a_queued_refund_is_not_sent_when_a_dispute_opens_before_the_worker_runs(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    rid = (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).json()["id"]
    await open_dispute(db_session, o["id"])

    await jobs(test_session_factory)
    await execute_refund({"refund_id": rid}, test_session_factory)  # a duplicate job changes nothing

    assert provider.refund_calls == []  # nothing reached the provider
    assert await one(db_session, "SELECT status FROM refunds") == "failed"
    assert "not submitted" in await one(db_session, "SELECT failure_reason FROM refunds")
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"  # never claimed refunded
    assert await kinds(db_session) == ["refund_blocked_by_dispute"]
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action = 'refund.blocked_by_dispute'") == 1


async def test_a_new_refund_request_is_refused_while_the_dispute_is_open_and_allowed_after_it_closes(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    await open_dispute(db_session, o["id"])
    blocked = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert blocked.status_code in (409, 422), blocked.text
    assert provider.refund_calls == []

    await db_session.execute(text("UPDATE disputes SET status = 'won', closed_at = now()"))
    await db_session.commit()
    ok = await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    assert ok.status_code == 202, ok.text
    await jobs(test_session_factory)
    assert len(provider.refund_calls) == 1
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"


async def test_a_retry_after_an_unknown_outcome_uses_the_same_key_and_records_the_overlap_when_a_dispute_appeared(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    rid = (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).json()["id"]
    provider.refund_mode = "timeout_after_action"  # the provider refunded, we never heard back
    with contextlib.suppress(PaymentProviderError):
        await execute_refund({"refund_id": rid}, test_session_factory)
    await open_dispute(db_session, o["id"])  # appears while the outcome is unknown

    provider.refund_mode = "succeeded"
    await execute_refund({"refund_id": rid}, test_session_factory)  # same idempotency key: learns the outcome, cannot double refund

    assert len({c["key"] for c in provider.refund_calls}) == 1 and len(provider.refund_ledger) == 1
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"  # the real outcome is recorded
    assert "dispute_during_refund" in await kinds(db_session)


async def test_a_dispute_recorded_after_the_refund_was_submitted_opens_a_reconciliation_record(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    provider.refund_mode = "pending"  # accepted by the provider, not final
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status FROM refunds") == "pending"

    event = WebhookEvent("evt_dp_h2", "charge.dispute.created", pi, 1000, None, "usd", provider_dispute_id="dp_h2_evt",
                         dispute_status="needs_response", dispute_reason="fraudulent")
    async with test_session_factory() as s:
        await _build_lifecycle(s).apply_dispute_event(event)
        await s.commit()

    assert await one(db_session, "SELECT status FROM disputes") == "open"
    assert await kinds(db_session) == ["dispute_during_refund", "dispute_opened"]
    assert len(provider.refund_calls) == 1  # the refund was neither repeated nor pretended to be prevented


async def test_refund_submission_and_dispute_recording_never_both_miss_each_other(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    """Race the two paths on the payment row 6 times: either the refund is held back, or the overlap is recorded."""
    outcomes = set()
    for i in range(6):
        limiter.reset()  # each iteration registers a buyer; the register limit is 5/minute
        h, o, p, pi = await paid_order(client, admin_headers, category, provider)
        rid = (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).json()["id"]
        event = WebhookEvent(f"evt_race_{i}", "charge.dispute.created", pi, 1000, None, "usd",
                             provider_dispute_id=f"dp_race_{i}", dispute_status="needs_response")

        async def submit(rid=rid):
            async with test_session_factory() as s:
                return await _build_lifecycle(s).begin_refund_attempt(uuid.UUID(rid))

        async def dispute(event=event):
            async with test_session_factory() as s:
                await _build_lifecycle(s).apply_dispute_event(event)
                await s.commit()

        attempt, _ = await asyncio.gather(submit(), dispute())
        found = await one(db_session, "SELECT array_agg(kind) FROM payment_reconciliations WHERE details::text LIKE :r", r=f"%{rid}%") or []
        dispute_rows = await one(db_session, "SELECT count(*) FROM disputes WHERE provider_dispute_id = :d", d=f"dp_race_{i}")
        assert dispute_rows == 1
        if attempt is None:  # the dispute won: the refund must not be submitted and must be accounted for
            assert "refund_blocked_by_dispute" in found
            assert await one(db_session, "SELECT status FROM refunds WHERE id = :r", r=rid) == "failed"
            outcomes.add("blocked")
        else:  # the refund won: it is being submitted, so the later dispute must flag the overlap
            overlap = await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'dispute_during_refund' "
                                "AND order_id = :o", o=o["id"])
            assert overlap == 1
            outcomes.add("submitted")
    print("race outcomes seen:", outcomes)
