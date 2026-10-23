# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""M3: two concurrent payment requests for one order against a provider that does not de-duplicate creation (Razorpay)."""

import asyncio

from test_order_payment_lifecycle import API, category, make_order, one, provider  # noqa: F401

from app.core.rate_limit import limiter


async def two_concurrent(client, headers, order_id, provider):
    async def slow_create():
        await asyncio.sleep(0.3)  # both requests are now past the planning phase before either records its payment

    provider.on_create = slow_create
    limiter.reset()
    return await asyncio.gather(*[client.post(f"{API}/payments", json={"order_id": order_id}, headers=headers) for _ in range(2)])


async def test_concurrent_requests_end_with_one_payment_and_the_unused_order_is_flagged_when_it_cannot_be_cancelled(
    client, admin_headers, category, provider, db_session
):
    provider.supports_cancel = False  # like Razorpay
    h, o, p = await make_order(client, admin_headers, category)
    first, second = await two_concurrent(client, h, o["id"], provider)

    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"]  # the caller always gets THE payment for the order
    assert await one(db_session, "SELECT count(*) FROM payments WHERE order_id = :o", o=o["id"]) == 1
    assert len(provider.created) == 2  # two provider orders were created ...
    flagged = await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'cancel_uncertain' AND order_id = :o", o=o["id"])
    assert flagged == 1  # ... and the one nobody can pay is in front of a human, not silently abandoned
    assert provider.cancel_calls == []  # (it cannot be cancelled at this provider)


async def test_with_a_provider_that_can_cancel_the_unused_intent_is_cancelled(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)
    first, second = await two_concurrent(client, h, o["id"], provider)

    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert await one(db_session, "SELECT count(*) FROM payments WHERE order_id = :o", o=o["id"]) == 1
    recorded = await one(db_session, "SELECT provider_payment_id FROM payments WHERE order_id = :o", o=o["id"])
    assert len(provider.cancel_calls) == 1 and provider.cancel_calls[0] != recorded  # the loser, never the recorded intent
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE order_id = :o", o=o["id"]) == 0
