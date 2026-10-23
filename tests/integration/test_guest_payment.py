# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""H3: a guest pays only with the order-scoped capability issued at checkout; nothing else authorises a guest payment."""

import hashlib

from sqlalchemy import text
from test_order_payment_lifecycle import ADDR, API, category, make_order, one, provider  # noqa: F401

from app.core.rate_limit import limiter

_n = {"i": 0}


async def guest_order(client, admin_headers, category, *, key=None):
    """Guest cart -> guest checkout. Returns (order_json, checkout_response, pay_cookie_value)."""
    _n["i"] += 1
    i = _n["i"]
    limiter.reset()
    client.cookies.clear()
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"GP {i}", "description": None, "price_cents": 1000, "sku": f"GP-{i}", "stock": 10,
        "category_id": category, "status": "active"})).json()
    assert (await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})).status_code in (200, 201)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR, "guest_email": f"g{i}@example.com"},
                          headers={"Idempotency-Key": key or f"gp-{i}"})
    assert r.status_code == 201, r.text
    order = r.json()
    name = f"guest_pay_{order['id'].replace('-', '')}"
    return order, r, r.cookies.get(name), name


def cookie(name, value):
    return {"Cookie": f"{name}={value}"}


async def test_checkout_issues_an_httponly_order_scoped_cookie_and_stores_only_its_hash(client, admin_headers, category, provider, db_session):
    order, r, token, name = await guest_order(client, admin_headers, category)
    assert token and len(token) >= 40
    header = next(h for h in r.headers.get_list("set-cookie") if h.startswith(name))
    assert "HttpOnly" in header and "SameSite=lax" in header and "Path=/api/v1/payments" in header and "Max-Age=1800" in header
    assert token not in r.text  # never in a response body
    stored = await one(db_session, "SELECT payment_token_hash FROM orders WHERE id = :o", o=order["id"])
    assert stored == hashlib.sha256(token.encode()).hexdigest() and stored != token


async def test_a_guest_pays_with_the_capability_and_a_repeat_reuses_the_same_payment(client, admin_headers, category, provider, db_session):
    order, _r, token, name = await guest_order(client, admin_headers, category)
    client.cookies.clear()
    first = await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=cookie(name, token))
    assert first.status_code == 201, first.text
    assert first.json()["amount_cents"] == order["total_cents"] and first.json()["client_secret"]
    assert token not in first.text
    again = await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=cookie(name, token))
    assert again.status_code == 201 and again.json()["id"] == first.json()["id"]
    assert await one(db_session, "SELECT count(*) FROM payments WHERE order_id = :o", o=order["id"]) == 1
    assert len(provider.created) == 1  # no duplicate provider intent


async def test_no_credential_is_401_and_the_order_id_alone_never_authorises(client, admin_headers, category, provider):
    order, _r, token, name = await guest_order(client, admin_headers, category)
    client.cookies.clear()
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]})).status_code == 401
    # the guest CART cookie (a different secret, not order-scoped) does not authorise payment either
    cart = (await client.post(f"{API}/cart/items", json={"product_id": 1, "quantity": 1})).cookies.get("guest_cart_token")
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=cookie("guest_cart_token", cart or "x"))).status_code == 401
    assert provider.created == []


async def test_wrong_or_other_orders_capability_is_an_indistinguishable_404(client, admin_headers, category, provider):
    a, _r, token_a, name_a = await guest_order(client, admin_headers, category)
    b, _r2, token_b, name_b = await guest_order(client, admin_headers, category)
    client.cookies.clear()
    wrong = await client.post(f"{API}/payments", json={"order_id": a["id"]}, headers=cookie(name_a, "not-the-token"))
    swapped = await client.post(f"{API}/payments", json={"order_id": a["id"]}, headers=cookie(name_a, token_b))  # B's capability on A
    unknown = await client.post(f"{API}/payments", json={"order_id": "00000000-0000-0000-0000-000000000000"},
                                headers=cookie("guest_pay_" + "0" * 32, token_a))
    assert wrong.status_code == swapped.status_code == unknown.status_code == 404
    assert wrong.json()["detail"] == swapped.json()["detail"] == unknown.json()["detail"]
    assert provider.created == []


async def test_a_guest_capability_cannot_pay_a_registered_users_order_and_users_cannot_pay_guest_orders(client, admin_headers, category, provider):
    h, user_order, _p = await make_order(client, admin_headers, category)
    guest, _r, token, name = await guest_order(client, admin_headers, category)
    client.cookies.clear()
    # a guest capability presented for a registered user's order
    assert (await client.post(f"{API}/payments", json={"order_id": user_order["id"]},
                              headers=cookie("guest_pay_" + user_order["id"].replace("-", ""), token))).status_code == 404
    # a signed-in user (or an admin) is not the guest and cannot pay the guest's order with their own token
    assert (await client.post(f"{API}/payments", json={"order_id": guest["id"]}, headers=h)).status_code == 403
    assert (await client.post(f"{API}/payments", json={"order_id": guest["id"]}, headers=admin_headers)).status_code == 403
    assert provider.created == []


async def test_the_capability_expires_with_the_unpaid_order_window_and_ends_with_cancellation(client, admin_headers, category, provider, db_session):
    order, _r, token, name = await guest_order(client, admin_headers, category)
    client.cookies.clear()
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '3 hours' WHERE id = :o"), {"o": order["id"]})
    await db_session.commit()
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=cookie(name, token))).status_code == 404

    fresh, _r2, token2, name2 = await guest_order(client, admin_headers, category)
    client.cookies.clear()
    await db_session.execute(text("UPDATE orders SET status = 'cancelled', stock_released_at = now() WHERE id = :o"), {"o": fresh["id"]})
    await db_session.commit()
    blocked = await client.post(f"{API}/payments", json={"order_id": fresh["id"]}, headers=cookie(name2, token2))
    assert blocked.status_code in (409, 422) and provider.created == []


async def test_a_replayed_checkout_rotates_the_capability(client, admin_headers, category, provider):
    order, _r, old, name = await guest_order(client, admin_headers, category, key="gp-replay")
    again = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR, "guest_email": f"g{_n['i']}@example.com"},
                              headers={"Idempotency-Key": "gp-replay"})
    assert again.status_code == 201 and again.json()["id"] == order["id"]
    new = again.cookies.get(name)
    assert new and new != old
    client.cookies.clear()
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=cookie(name, old))).status_code == 404
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=cookie(name, new))).status_code == 201


async def test_registered_user_checkout_sets_no_guest_cookie_and_user_payment_still_works(client, admin_headers, category, provider):
    h, order, _p = await make_order(client, admin_headers, category)
    assert not any(c.startswith("guest_pay_") for c in list(client.cookies))
    r = await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=h)
    assert r.status_code == 201
