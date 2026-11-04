# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""POST /cart/quote: what checkout would charge. The core guarantee is PARITY: for the same cart, coupon and address the quote's
subtotal, discount, shipping, tax and total equal what a real checkout then charges."""

from decimal import Decimal

import pytest
from test_cart_variants import add, cart, signup
from test_coupons import fill, mk, place, shop
from test_order_payment_lifecycle import ADDR, category, hook, jobs, pay, provider  # noqa: F401

from app.core.config import get_settings

API = "/api/v1"
FIELDS = ("subtotal_cents", "discount_cents", "shipping_cents", "total_cents")


async def quote(client, headers=None, **body):
    r = await client.post(f"{API}/cart/quote", json=body, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(autouse=True)
def pricing(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", 5000)
    monkeypatch.setattr(s, "free_shipping_threshold_cents", None)
    monkeypatch.setattr(s, "free_shipping_threshold_basis", "subtotal")
    monkeypatch.setattr(s, "tax_rate_percent", Decimal("18"))


SCENARIOS = {
    "plain": dict(coupon=None),
    "percentage coupon": dict(coupon=("PCT10", {})),
    "fixed coupon": dict(coupon=("FIX", dict(kind="fixed", percent_off=None, amount_off_cents=7777))),
    "free shipping coupon": dict(coupon=("FREE", dict(kind="free_shipping", percent_off=None))),
    "free shipping by threshold": dict(coupon=None, threshold=90000),
    "threshold missed after the discount": dict(coupon=("PCT10", {}), threshold=95000),
    "three lines, rounding": dict(coupon=("PCT7", dict(percent_off="7")), lines=(33333, 33333, 33334)),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_the_quote_equals_what_checkout_then_charges(client, admin_headers, category, monkeypatch, name):
    spec = SCENARIOS[name]
    if "threshold" in spec:
        monkeypatch.setattr(get_settings(), "free_shipping_threshold_cents", spec["threshold"])
    headers, _ = await signup(client)
    for price in spec.get("lines", (100000,)):
        await fill(client, headers, (await shop(client, admin_headers, category, price=price))["id"], qty=1)
    code = None
    if spec["coupon"]:
        code, extra = spec["coupon"]
        await mk(client, admin_headers, code, **extra)

    q = await quote(client, headers, shipping_address=ADDR, **({"coupon_code": code} if code else {}))
    order = (await place(client, headers, code)).json()

    assert {f: q[f] for f in FIELDS} == {f: order[f] for f in FIELDS}
    assert q["tax"]["added_cents"] == order["tax_cents"] and q["tax"]["status"] == "live"
    assert sum(line["discount_cents"] for line in q["lines"]) == order["discount_cents"]
    assert q["blocking"] == [] and q["price_changed"] is False


async def test_a_quote_changes_nothing(client, admin_headers, category, db_session):
    headers, _ = await signup(client)
    pid = (await shop(client, admin_headers, category, price=100000, stock=5))["id"]
    await mk(client, admin_headers, "PCT10")
    await fill(client, headers, pid, qty=2)
    before = await cart(client, headers)
    for _ in range(2):
        await quote(client, headers, shipping_address=ADDR, coupon_code="pct10")
    assert await cart(client, headers) == before  # the same lines, snapshots and digest
    from sqlalchemy import text

    assert (await db_session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0  # a coupon is used up only by an order
    assert (await db_session.execute(text("SELECT stock FROM products WHERE id = :p"), {"p": pid})).scalar_one() == 5


async def test_it_works_for_a_guest_and_without_an_address(client, admin_headers, category):
    pid = (await shop(client, admin_headers, category, price=100000))["id"]
    assert (await add(client, product_id=pid, quantity=1)).status_code == 201
    q = await quote(client)  # no address, no coupon: the flat interim tax is on the goods and does not depend on the address
    assert (q["subtotal_cents"], q["shipping_cents"], q["tax"]["added_cents"], q["total_cents"], q["requires_address"]) == (100000, 5000, 18000, 123000, False)
    assert (q["currency"], q["minor_unit_exponent"]) == ("usd", 2)


async def test_an_empty_cart_quotes_nothing_and_does_not_fail(client):
    q = await quote(client)
    assert (q["lines"], q["subtotal_cents"], q["total_cents"], q["shipping_reason"]) == ([], 0, None, "cart_empty")


async def test_a_bad_coupon_is_refused_with_its_own_code_like_checkout(client, admin_headers, category):
    headers, _ = await signup(client)
    await fill(client, headers, (await shop(client, admin_headers, category))["id"], qty=1)
    r = await client.post(f"{API}/cart/quote", json={"coupon_code": "NOPE"}, headers=headers)
    assert r.status_code == 422 and r.json()["code"] == "coupon_invalid"


async def test_unset_shipping_or_tax_is_reported_not_guessed(client, admin_headers, category, monkeypatch):
    headers, _ = await signup(client)
    await fill(client, headers, (await shop(client, admin_headers, category, price=100000))["id"], qty=1)
    monkeypatch.setattr(get_settings(), "flat_shipping_cents", None)
    q = await quote(client, headers)
    assert (q["shipping_cents"], q["shipping_reason"], q["total_cents"], q["subtotal_cents"]) == (None, "not_configured", None, 100000)
    monkeypatch.setattr(get_settings(), "flat_shipping_cents", 5000)
    monkeypatch.setattr(get_settings(), "tax_rate_percent", None)
    q = await quote(client, headers)
    assert (q["tax"]["status"], q["tax"]["engine"], q["tax"]["added_cents"], q["tax"]["reason"], q["total_cents"]) == ("not_active", "not_configured", None, "not_configured", None)


async def test_current_prices_blocking_lines_and_price_changes_are_visible(client, admin_headers, category, db_session):
    from sqlalchemy import text

    headers, _ = await signup(client)
    a = (await shop(client, admin_headers, category, price=100000, stock=10))["id"]
    b = (await shop(client, admin_headers, category, price=50000, stock=10))["id"]
    await fill(client, headers, a, qty=1)
    await fill(client, headers, b, qty=2)
    await db_session.execute(text("UPDATE products SET price_cents = 120000 WHERE id = :p"), {"p": a})
    await db_session.execute(text("UPDATE products SET stock = 0 WHERE id = :p"), {"p": b})
    await db_session.commit()
    q = await quote(client, headers)
    assert q["subtotal_cents"] == 120000 + 100000 and q["price_changed"] is True  # CURRENT prices, as checkout would charge
    assert [(x["reason"]) for x in q["blocking"]] == ["out_of_stock"]
    assert (await cart(client, headers))["subtotal_cents"] == 200000  # the cart itself still shows what the customer saw


async def test_an_address_that_cannot_be_used_is_refused_and_a_foreign_saved_address_is_not_found(client, admin_headers, category):
    headers, _ = await signup(client)
    await fill(client, headers, (await shop(client, admin_headers, category))["id"], qty=1)
    r = await client.post(f"{API}/cart/quote", json={"address_id": 999999}, headers=headers)
    assert r.status_code == 404
    r = await client.post(f"{API}/cart/quote", json={"address_id": 1, "shipping_address": ADDR}, headers=headers)
    assert r.status_code == 422  # one source only, like checkout
