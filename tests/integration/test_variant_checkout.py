# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""E4: checkout and the order lifecycle with variants: authoritative price and stock, frozen order lines, replay protection, concurrency,
and restocking exactly once per order line."""

import asyncio
import uuid

from sqlalchemy import text
from test_cart_variants import add, cart, set_variant, signup
from test_order_payment_lifecycle import (  # noqa: F401
    ADDR,
    SWEEP_STALE_ORDERS,
    category,
    enqueue_due,
    hook,
    jobs,
    make_stale,
    pay,
    provider,
)
from test_stock_ledger import assert_balanced
from test_variant_pricing import option_product, simple_product

from app.core.config import get_settings
from app.modules.orders.repositories.order_repository import OrderRepository

API = "/api/v1"


async def checkout(client, headers, key=None, address=ADDR):
    return await client.post(f"{API}/orders/checkout", json={"shipping_address": address}, headers={**(headers or {}), "Idempotency-Key": key or uuid.uuid4().hex})


async def vstock(db, variant_id):
    await db.rollback()
    return (await db.execute(text("SELECT stock FROM product_variants WHERE id = :v"), {"v": variant_id})).scalar_one()


async def count(db, sql, **params):
    await db.rollback()
    return (await db.execute(text(sql), params)).scalar_one()


# ============================================================================================== selling variants
async def test_a_variant_order_is_priced_from_the_locked_variant_moves_its_stock_and_freezes_each_line(client, db_session, category, admin_headers):
    pid, (a, b) = await option_product(db_session, category, [(700, 5, "active"), (None, 5, "active")], base=1000, name="Tee")
    plain = await simple_product(db_session, category, price=300, stock=4)
    headers, _ = await signup(client)
    for body in ({"variant_id": a, "quantity": 2}, {"variant_id": b, "quantity": 1}, {"product_id": plain, "quantity": 1}):
        assert (await add(client, headers, **body)).status_code == 201
    r = await checkout(client, headers)
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["subtotal_cents"] == 2 * 700 + 1000 + 300
    lines = {i["variant_id"]: i for i in order["items"]}
    assert lines[a]["price_cents_snapshot"] == 700 and lines[a]["variant"] == {"sku": lines[a]["sku"], "options": [{"name": "Size", "value": "S1"}], "price_source": "override"}
    assert lines[b]["price_cents_snapshot"] == 1000 and lines[b]["variant"]["price_source"] == "product" and lines[b]["sku"].startswith("PV-")
    assert lines[None]["variant"] is None and lines[None]["product_name_snapshot"] and lines[None]["sku"]       # the simple line is unchanged
    assert (await vstock(db_session, a), await vstock(db_session, b)) == (3, 4)                               # each variant moved, nothing else
    assert await count(db_session, "SELECT stock FROM products WHERE id = :p", p=plain) == 3
    assert await count(db_session, "SELECT stock FROM products WHERE id = :p", p=pid) == 0                    # the parent holds no stock
    assert (await cart(client, headers))["items"] == []                                                       # the cart was emptied
    sales = (await db_session.execute(text("SELECT kind, delta, order_id::text, variant_id FROM stock_movements WHERE kind = 'sale' ORDER BY id"))).all()
    assert {(r[0], r[2]) for r in sales} == {("sale", order["id"])} and sorted(r[1] for r in sales) == [-2, -1, -1]   # the ledger names the order
    await assert_balanced(db_session)


async def test_order_history_is_not_rewritten_by_later_changes(client, db_session, category, admin_headers):
    pid, (a, b) = await option_product(db_session, category, [(700, 5, "active"), (None, 5, "active")], name="Hoodie")
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=1)
    order = (await checkout(client, headers)).json()
    before = (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["items"][0]

    await set_variant(db_session, a, price_override_cents=999, status="inactive")                  # price edited, variant retired
    await db_session.execute(text("UPDATE product_option_values SET value = 'Renamed' WHERE id = (SELECT value1_id FROM product_variants WHERE id = :v)"), {"v": a})
    await db_session.execute(text("UPDATE products SET name = 'Totally Different', price_cents = 5000 WHERE id = :p"), {"p": pid})
    await db_session.commit()

    after = (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()
    assert after["items"][0] == before and after["subtotal_cents"] == order["subtotal_cents"] == 700
    assert before["variant"]["options"] == [{"name": "Size", "value": "S1"}] and before["product_name_snapshot"] == "Hoodie"


async def test_a_price_change_asks_for_review_names_the_variant_and_charges_the_new_price_next_time(client, db_session, category):
    pid, (a,) = await option_product(db_session, category, [(700, 5, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=2)
    await set_variant(db_session, a, price_override_cents=900)
    first = await checkout(client, headers, key="price-1")
    assert first.status_code == 409 and first.json()["code"] == "price_changed"
    change = first.json()["changes"][0]
    assert change["variant_id"] == a and change["old_price_cents"] == 700 and change["new_price_cents"] == 900 and change["sku"].startswith("PV-")
    assert await vstock(db_session, a) == 5 and await count(db_session, "SELECT count(*) FROM orders") == 0      # nothing was taken
    assert (await cart(client, headers))["items"][0]["price_cents_snapshot"] == 900                          # the cart now shows the real price
    second = await checkout(client, headers, key="price-2")
    assert second.status_code == 201 and second.json()["subtotal_cents"] == 1800


async def test_anything_that_cannot_be_sold_at_checkout_is_refused_and_changes_nothing(client, db_session, category):
    pid, (inactive, scarce, fine) = await option_product(db_session, category, [(None, 9, "active")] * 3)
    other, (late,) = await option_product(db_session, category, [(None, 9, "active")])
    cases = []
    for variant, mutate in ((inactive, {"status": "inactive"}), (scarce, {"stock": 1})):
        headers, _ = await signup(client)
        await add(client, headers, variant_id=variant, quantity=3)
        await set_variant(db_session, variant, **mutate)
        cases.append((headers, variant))
    headers, _ = await signup(client)
    await add(client, headers, variant_id=late, quantity=1)
    await db_session.execute(text("UPDATE products SET status = 'archived' WHERE id = :p"), {"p": other})
    await db_session.commit()
    cases.append((headers, late))
    results = [await checkout(client, h) for h, _v in cases]
    assert [r.status_code for r in results] == [422, 422, 422]
    assert results[0].json()["code"] == results[2].json()["code"] == "variant_unavailable"      # inactive variant; archived product
    assert "Only 1 unit(s)" in results[1].json()["detail"]                                       # 3 wanted, 1 left
    for (h, v), expected in zip(cases, (9, 1, 9), strict=True):
        assert await vstock(db_session, v) == expected                                                          # stock untouched
        assert len((await cart(client, h))["items"]) == 1                                                       # cart intact
    assert await count(db_session, "SELECT count(*) FROM orders") == 0


# ============================================================================================== concurrency
async def test_two_buyers_for_the_last_unit_get_one_order_and_no_oversell(client, db_session, category):
    pid, (v,) = await option_product(db_session, category, [(None, 1, "active")])
    buyers = [(await signup(client))[0] for _ in range(3)]
    for h in buyers:
        assert (await add(client, h, variant_id=v, quantity=1)).status_code == 201     # each sees it in stock when adding
    results = await asyncio.gather(*[checkout(client, h) for h in buyers])
    assert sorted(r.status_code for r in results) == [201, 422, 422]
    assert await vstock(db_session, v) == 0 and await count(db_session, "SELECT count(*) FROM orders") == 1
    await assert_balanced(db_session)


async def test_checkout_waits_for_a_concurrent_deactivation_and_then_refuses(client, db_session, test_session_factory, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 5, "active"), (None, 5, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=1)
    s = test_session_factory()
    try:
        await s.execute(text("UPDATE product_variants SET status = 'inactive' WHERE id = :v"), {"v": a})   # holds the product row until commit
        task = asyncio.create_task(checkout(client, headers))
        await asyncio.sleep(0.6)
        assert not task.done()                                  # it is waiting for the lock, not racing past it
        await s.commit()
        r = await task
    finally:
        await s.close()
    assert r.status_code == 422 and r.json()["code"] == "variant_unavailable"
    assert await vstock(db_session, a) == 5 and await count(db_session, "SELECT count(*) FROM orders") == 0


async def test_overlapping_multi_variant_carts_in_opposite_orders_never_deadlock(client, db_session, category):
    pid, ids = await option_product(db_session, category, [(None, 100, "active")] * 3)
    users = []
    for n in range(6):
        h, _ = await signup(client)
        for v in (ids if n % 2 == 0 else list(reversed(ids))):
            await add(client, h, variant_id=v, quantity=1)
        users.append(h)
    results = await asyncio.gather(*[checkout(client, h) for h in users])
    assert [r.status_code for r in results] == [201] * 6
    assert [await vstock(db_session, v) for v in ids] == [94, 94, 94]
    await assert_balanced(db_session)


# ============================================================================================== idempotency
async def test_a_retried_key_returns_the_same_order_and_a_reused_key_with_a_different_cart_is_refused(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 20, "active"), (None, 20, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=2)
    first = await checkout(client, headers, key="K")
    assert first.status_code == 201
    retry = await checkout(client, headers, key="K")                                   # the cart is empty: the normal retry
    assert retry.status_code in (200, 201) and retry.json()["id"] == first.json()["id"]
    assert await vstock(db_session, a) == 18                                           # charged once

    await add(client, headers, variant_id=a, quantity=2)                               # the client re-adds the SAME lines and retries
    same = await checkout(client, headers, key="K")
    assert same.status_code in (200, 201) and same.json()["id"] == first.json()["id"]
    await add(client, headers, variant_id=b, quantity=1)                               # now the cart differs from what the key was used for
    different = await checkout(client, headers, key="K")
    assert different.status_code == 409 and different.json()["code"] == "idempotency_key_reused"
    assert len((await cart(client, headers))["items"]) == 2                            # the new cart is untouched
    assert await count(db_session, "SELECT count(*) FROM orders") == 1 and await vstock(db_session, a) == 18 and await vstock(db_session, b) == 20
    await client.delete(f"{API}/cart", headers=headers)
    other_address = await checkout(client, headers, key="K", address={**ADDR, "line1": "Somewhere Else 9"})
    assert other_address.status_code == 409 and other_address.json()["code"] == "idempotency_key_reused"      # as before


async def test_an_order_from_before_digests_existed_keeps_the_old_replay_behaviour(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 20, "active"), (None, 20, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=1)
    first = await checkout(client, headers, key="LEGACY")
    await db_session.execute(text("UPDATE orders SET cart_digest = NULL"))                # an order made before this feature
    await db_session.commit()
    await add(client, headers, variant_id=b, quantity=3)
    again = await checkout(client, headers, key="LEGACY")
    assert again.status_code in (200, 201) and again.json()["id"] == first.json()["id"]   # compared on the address only, as it always was


async def test_a_guest_reusing_a_key_with_a_different_cart_is_refused_too(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 20, "active"), (None, 20, "active")])
    client.cookies.clear()
    await add(client, None, variant_id=a, quantity=1)
    body = {"shipping_address": ADDR, "guest_email": "g@example.com"}
    first = await client.post(f"{API}/orders/checkout", json=body, headers={"Idempotency-Key": "G1"})
    assert first.status_code == 201
    await add(client, None, variant_id=b, quantity=1)
    clash = await client.post(f"{API}/orders/checkout", json=body, headers={"Idempotency-Key": "G1"})
    assert clash.status_code == 409 and clash.json()["code"] == "idempotency_key_reused"
    assert await count(db_session, "SELECT count(*) FROM orders") == 1


async def test_two_simultaneous_requests_with_one_key_make_one_order(client, db_session, category):
    pid, (a,) = await option_product(db_session, category, [(None, 20, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=2)
    results = await asyncio.gather(*[checkout(client, headers, key="SAME") for _ in range(3)])
    assert {r.json()["id"] for r in results if r.status_code in (200, 201)} and len({r.json()["id"] for r in results}) == 1
    assert await count(db_session, "SELECT count(*) FROM orders") == 1 and await vstock(db_session, a) == 18


# ============================================================================================== restocking
async def _released(db, order_id):
    await db.rollback()
    return (await db.execute(text(
        "SELECT oi.quantity, oi.restocked_quantity, (SELECT count(*) FROM order_item_restocks r WHERE r.order_item_id = oi.id), o.stock_released_at IS NOT NULL "
        "FROM order_items oi JOIN orders o ON o.id = oi.order_id WHERE oi.order_id = :o ORDER BY oi.id"), {"o": order_id})).all()


async def test_cancelling_an_unpaid_variant_order_returns_the_stock_exactly_once(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 10, "active"), (None, 10, "active")])
    plain = await simple_product(db_session, category, stock=10)
    headers, _ = await signup(client)
    for body in ({"variant_id": a, "quantity": 3}, {"variant_id": b, "quantity": 2}, {"product_id": plain, "quantity": 4}):
        await add(client, headers, **body)
    order = (await checkout(client, headers)).json()
    assert (await vstock(db_session, a), await vstock(db_session, b)) == (7, 8)
    assert (await client.post(f"{API}/orders/{order['id']}/cancel", headers=headers)).status_code == 200
    assert (await vstock(db_session, a), await vstock(db_session, b)) == (10, 10) and await count(db_session, "SELECT stock FROM products WHERE id = :p", p=plain) == 10
    assert [(q, rq, events, flag) for q, rq, events, flag in await _released(db_session, order["id"])] == [(3, 3, 1, True), (2, 2, 1, True), (4, 4, 1, True)]
    await client.post(f"{API}/orders/{order['id']}/cancel", headers=headers)                      # a second attempt changes nothing
    assert (await vstock(db_session, a), await vstock(db_session, b)) == (10, 10)
    restocks = (await db_session.execute(text("SELECT kind, delta, order_id::text FROM stock_movements WHERE kind = 'restock'"))).all()
    assert sorted(r[1] for r in restocks) == [2, 3, 4] and {r[2] for r in restocks} == {order["id"]}
    await assert_balanced(db_session)


async def test_replaying_a_restock_event_never_adds_stock_twice_or_beyond_what_was_bought(client, db_session, category):
    pid, (a,) = await option_product(db_session, category, [(None, 10, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=3)
    order = (await checkout(client, headers)).json()
    await client.post(f"{API}/orders/{order['id']}/cancel", headers=headers)
    item = await count(db_session, "SELECT id FROM order_items WHERE order_id = :o", o=order["id"])
    repo = OrderRepository(db_session)
    assert await repo.record_restock_event(item, "order-release", 3) is False             # the same event again: recorded already
    await db_session.rollback()
    assert (await _released(db_session, order["id"]))[0][:3] == (3, 3, 1)


async def test_a_paid_variant_order_cancelled_with_a_refund_restocks_once_after_the_provider_confirms(client, db_session, category, admin_headers, provider, test_session_factory):
    pid, (a,) = await option_product(db_session, category, [(None, 10, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=2)
    order = (await checkout(client, headers)).json()
    await pay(client, headers, order["id"])
    assert (await hook(client, provider.last_pi)).status_code == 200
    assert (await client.post(f"{API}/admin/orders/{order['id']}/cancel-and-refund", headers=admin_headers)).status_code == 202
    assert await vstock(db_session, a) == 8                                              # not yet: stock returns only when the refund is confirmed
    await jobs(test_session_factory)
    assert await vstock(db_session, a) == 10
    await jobs(test_session_factory)
    assert await vstock(db_session, a) == 10 and (await _released(db_session, order["id"]))[0][:3] == (2, 2, 1)
    await assert_balanced(db_session)


async def test_refunding_a_shipped_variant_order_does_not_restock_it(client, db_session, category, admin_headers, provider, test_session_factory):
    pid, (a,) = await option_product(db_session, category, [(None, 10, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=2)
    order = (await checkout(client, headers)).json()
    await pay(client, headers, order["id"])
    await hook(client, provider.last_pi)
    assert (await client.patch(f"{API}/orders/{order['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)).status_code == 200
    assert (await client.post(f"{API}/payments/{order['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert await vstock(db_session, a) == 8 and await count(db_session, "SELECT count(*) FROM order_item_restocks") == 0   # the goods left the shop


async def test_an_expired_unpaid_variant_order_returns_its_stock(client, db_session, category, provider, test_session_factory):
    pid, (a,) = await option_product(db_session, category, [(None, 10, "active")])
    headers, _ = await signup(client)
    await add(client, headers, variant_id=a, quantity=4)
    order = (await checkout(client, headers)).json()
    await pay(client, headers, order["id"])
    await make_stale(db_session, order["id"])
    assert SWEEP_STALE_ORDERS in await enqueue_due(test_session_factory, get_settings())
    await jobs(test_session_factory)
    assert await vstock(db_session, a) == 10
    assert (await _released(db_session, order["id"]))[0] == (4, 4, 1, True)
    await jobs(test_session_factory)
    assert await vstock(db_session, a) == 10                                              # a later sweep changes nothing
