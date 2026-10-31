# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""Coupons: the administrator's management, a customer's preview, checkout totals and tax, limits (also under simultaneous orders), and reports."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_cart_variants import add, signup
from test_gst_checkout import configure, eighteen
from test_invoices import fulfil, set_profile
from test_order_payment_lifecycle import ADDR, category, hook, jobs, pay, provider  # noqa: F401
from test_tax_engine_boundary import admin2, gst_on  # noqa: F401
from test_variant_admin import make_product

from app.core.config import get_settings

API = "/api/v1"


@pytest.fixture(autouse=True)
def _shipping(monkeypatch):
    monkeypatch.setattr(get_settings(), "flat_shipping_cents", 5000)
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_cents", None)


def body(code="SAVE10", **over):
    base = {"code": code, "name": "Test coupon", "kind": "percentage", "percent_off": "10"}
    return {**base, **over}


async def mk(client, admin_headers, code="SAVE10", **over):
    r = await client.post(f"{API}/admin/coupons", json=body(code, **over), headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


async def shop(client, admin_headers, category, price=100000, stock=20):
    return await make_product(client, admin_headers, category, price=price, stock=stock, status="active")


async def fill(client, headers, product_id, qty=1):
    assert (await add(client, headers, product_id=product_id, quantity=qty)).status_code == 201


async def place(client, headers, coupon=None, key=None, address=ADDR, **extra):
    payload = {"shipping_address": address, **({"coupon_code": coupon} if coupon else {}), **extra}
    return await client.post(f"{API}/orders/checkout", json=payload, headers={**(headers or {}), "Idempotency-Key": key or uuid.uuid4().hex})


async def preview(client, headers, code, **extra):
    return await client.post(f"{API}/cart/coupon/preview", json={"code": code, **extra}, headers=headers or {})


# ============================================================================================== administration
async def test_only_administrators_manage_coupons_and_a_coupon_must_be_well_formed(client, admin_headers):
    customer, _ = await signup(client)
    assert (await client.post(f"{API}/admin/coupons", json=body(), headers=customer)).status_code == 403
    assert (await client.get(f"{API}/admin/coupons", headers=customer)).status_code == 403
    bad = [
        {"code": "ab"}, {"code": "has space"}, {"code": "-LEADING"}, {"code": "X" * 33}, {"percent_off": "0"}, {"percent_off": "100.01"},
        {"percent_off": None}, {"amount_off_cents": 500}, {"kind": "fixed"}, {"kind": "fixed", "percent_off": None, "amount_off_cents": 0},
        {"kind": "fixed", "percent_off": None, "amount_off_cents": 500, "max_discount_cents": 100},
        {"kind": "free_shipping", "percent_off": "10"}, {"kind": "mystery"}, {"min_subtotal_cents": -1}, {"max_uses_total": 0},
        {"max_uses_per_customer": 0}, {"starts_at": "2030-01-02T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z"}, {"extra": 1}, {"max_discount_cents": 0},
    ]
    for over in bad:
        assert (await client.post(f"{API}/admin/coupons", json={**body("OK-CODE"), **over}, headers=admin_headers)).status_code == 422, over
    assert (await client.post(f"{API}/admin/coupons", json=body("free-ship", kind="free_shipping", percent_off=None), headers=admin_headers)).status_code == 201
    created = await mk(client, admin_headers, "  welcome10 ")
    assert created["code"] == "WELCOME10" and created["uses"] == 0 and created["state"] == "active"                    # stored in capitals
    dup = await client.post(f"{API}/admin/coupons", json=body("Welcome10"), headers=admin_headers)
    assert dup.status_code == 409 and dup.json()["code"] == "coupon_code_exists"                                      # codes are case-insensitive


async def test_the_coupon_list_shows_states_and_filters(client, admin_headers):
    now = datetime.now(UTC)
    await mk(client, admin_headers, "ACTIVE1")
    await mk(client, admin_headers, "OFF1", active=False)
    await mk(client, admin_headers, "LATER1", starts_at=(now + timedelta(days=5)).isoformat())
    await mk(client, admin_headers, "ENDED1", starts_at=(now - timedelta(days=9)).isoformat(), ends_at=(now - timedelta(days=1)).isoformat())
    states = {c["code"]: c["state"] for c in (await client.get(f"{API}/admin/coupons", headers=admin_headers)).json()["items"]}
    assert states == {"ACTIVE1": "active", "OFF1": "disabled", "LATER1": "scheduled", "ENDED1": "expired"}
    assert [c["code"] for c in (await client.get(f"{API}/admin/coupons", params={"state": "expired"}, headers=admin_headers)).json()["items"]] == ["ENDED1"]
    assert (await client.get(f"{API}/admin/coupons", params={"search": "later"}, headers=admin_headers)).json()["total"] == 1
    assert (await client.get(f"{API}/admin/coupons", params={"state": "bogus"}, headers=admin_headers)).status_code == 422


async def test_what_a_coupon_gives_is_locked_once_it_has_been_used_and_only_an_unused_one_can_be_deleted(client, admin_headers, category):
    p = await shop(client, admin_headers, category)
    fresh = await mk(client, admin_headers, "FRESH1")
    used = await mk(client, admin_headers, "USED1")
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    assert (await place(client, headers, "used1")).status_code == 201
    edit_fresh = await client.patch(f"{API}/admin/coupons/{fresh['id']}", json={"percent_off": "15", "name": "Renamed"}, headers=admin_headers)
    assert edit_fresh.status_code == 200 and edit_fresh.json()["percent_off"] == "15.00" and edit_fresh.json()["name"] == "Renamed"
    locked = await client.patch(f"{API}/admin/coupons/{used['id']}", json={"percent_off": "50"}, headers=admin_headers)
    assert locked.status_code == 409 and locked.json()["code"] == "coupon_in_use"
    assert (await client.patch(f"{API}/admin/coupons/{used['id']}", json={"active": False, "max_uses_total": 5, "ends_at": None}, headers=admin_headers)).status_code == 200
    inconsistent = await client.patch(f"{API}/admin/coupons/{fresh['id']}", json={"kind": "fixed"}, headers=admin_headers)    # a fixed coupon needs its amount
    assert inconsistent.status_code == 422 and inconsistent.json()["code"] == "coupon_settings_invalid"
    assert (await client.patch(f"{API}/admin/coupons/{fresh['id']}", json={"starts_at": "2031-01-02T00:00:00Z", "ends_at": "2031-01-01T00:00:00Z"}, headers=admin_headers)).status_code == 422
    assert (await client.delete(f"{API}/admin/coupons/{used['id']}", headers=admin_headers)).status_code == 409
    assert (await client.delete(f"{API}/admin/coupons/{fresh['id']}", headers=admin_headers)).status_code == 204
    assert (await client.get(f"{API}/admin/coupons/{fresh['id']}", headers=admin_headers)).status_code == 404
    page = (await client.get(f"{API}/admin/coupons/{used['id']}/redemptions", headers=admin_headers)).json()
    assert page["total"] == 1 and page["items"][0]["discount_cents"] == 10000 and page["items"][0]["status"] == "pending"


# ============================================================================================== preview
async def test_a_customer_can_see_what_a_coupon_does_to_their_cart_before_buying(client, admin_headers, category):
    p = await shop(client, admin_headers, category, price=100000)
    await mk(client, admin_headers, "TENOFF", min_subtotal_cents=50000)
    await mk(client, admin_headers, "FLAT500", kind="fixed", percent_off=None, amount_off_cents=50000)
    await mk(client, admin_headers, "SHIPFREE", kind="free_shipping", percent_off=None)
    headers, _ = await signup(client)
    empty = await preview(client, headers, "TENOFF")
    assert empty.status_code == 422 and empty.json()["code"] == "coupon_not_applicable"
    await fill(client, headers, p["id"], 2)
    pct = (await preview(client, headers, "tenoff")).json()                                                              # any letter case
    assert pct == {"code": "TENOFF", "kind": "percentage", "subtotal_cents": 200000, "discount_cents": 20000, "subtotal_after_discount_cents": 180000,
                   "shipping_cents": 5000, "shipping_discount_cents": 0}
    assert (await preview(client, headers, "FLAT500")).json()["discount_cents"] == 50000
    free = (await preview(client, headers, "SHIPFREE")).json()
    assert (free["discount_cents"], free["shipping_cents"], free["shipping_discount_cents"]) == (0, 0, 5000)
    unknown = await preview(client, headers, "NOPE")
    assert unknown.status_code == 422 and unknown.json()["code"] == "coupon_invalid"
    assert (await client.get(f"{API}/cart", headers=headers)).json()["subtotal_cents"] == 200000                        # a preview changes nothing


async def test_a_coupon_that_does_not_apply_says_why_and_expired_or_switched_off_ones_look_unknown(client, admin_headers, category):
    p = await shop(client, admin_headers, category, price=10000)
    now = datetime.now(UTC)
    await mk(client, admin_headers, "BIGONLY", min_subtotal_cents=50000)
    await mk(client, admin_headers, "OFF2", active=False)
    await mk(client, admin_headers, "LATER2", starts_at=(now + timedelta(days=2)).isoformat())
    await mk(client, admin_headers, "ENDED2", starts_at=(now - timedelta(days=9)).isoformat(), ends_at=(now - timedelta(days=1)).isoformat())
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    small = await preview(client, headers, "BIGONLY")
    assert small.status_code == 422 and small.json()["code"] == "coupon_minimum_not_met"
    for code in ("OFF2", "LATER2", "ENDED2", "NEVER"):
        r = await preview(client, headers, code)
        assert r.status_code == 422 and r.json()["code"] == "coupon_invalid", code                                          # nothing is revealed about codes
    free_already = await mk(client, admin_headers, "SHIPFREE2", kind="free_shipping", percent_off=None)
    assert free_already["state"] == "active"


async def test_a_guest_can_preview_and_per_customer_limits_are_checked_by_email(client, admin_headers, category):
    p = await shop(client, admin_headers, category)
    await mk(client, admin_headers, "ONCE", max_uses_per_customer=1)
    client.cookies.clear()
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})
    assert (await preview(client, None, "ONCE", guest_email="Guest@Example.com")).status_code == 200
    order = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR, "guest_email": "guest@example.com", "coupon_code": "once"}, headers={"Idempotency-Key": "g1"})
    assert order.status_code == 201 and order.json()["discount_cents"] == 10000
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})
    again = await preview(client, None, "ONCE", guest_email="GUEST@example.com")                                            # the same email, another case
    assert again.status_code == 422 and again.json()["code"] == "coupon_limit_reached"
    assert (await preview(client, None, "ONCE", guest_email="someone.else@example.com")).status_code == 200


# ============================================================================================== checkout
async def test_a_percentage_coupon_reduces_the_goods_and_the_totals_add_up(client, admin_headers, category, db_session):
    p = await shop(client, admin_headers, category, price=100000)
    coupon = await mk(client, admin_headers, "TENOFF")
    headers, _ = await signup(client)
    await fill(client, headers, p["id"], 2)
    r = await place(client, headers, "tenoff")
    assert r.status_code == 201, r.text
    o = r.json()
    assert (o["subtotal_cents"], o["discount_cents"], o["shipping_cents"], o["total_cents"], o["coupon_code"]) == (200000, 20000, 5000, 200000 - 20000 + 5000 + o["tax_cents"], "TENOFF")
    assert o["items"][0]["discount_cents"] == 20000 and o["items"][0]["line_total_cents"] == 200000
    snap = (await db_session.execute(text("SELECT coupon_snapshot FROM orders WHERE id = :o"), {"o": o["id"]})).scalar_one()
    assert snap["code"] == "TENOFF" and snap["kind"] == "percentage" and snap["percent_off"] == "10.00" and snap["goods_discount_cents"] == 20000
    assert (await client.get(f"{API}/admin/coupons/{coupon['id']}", headers=admin_headers)).json()["uses"] == 1


async def test_the_discount_is_shared_over_the_lines_exactly(client, admin_headers, category):
    a = await shop(client, admin_headers, category, price=60000)
    b = await shop(client, admin_headers, category, price=40000)
    await mk(client, admin_headers, "SHARE", percent_off="3.33")
    headers, _ = await signup(client)
    await fill(client, headers, a["id"])
    await fill(client, headers, b["id"])
    o = (await place(client, headers, "SHARE")).json()
    assert o["discount_cents"] == 3330 and sorted(i["discount_cents"] for i in o["items"]) == [1332, 1998] and sum(i["discount_cents"] for i in o["items"]) == 3330


async def test_a_percentage_cap_a_fixed_amount_that_is_too_big_and_a_free_shipping_coupon(client, admin_headers, category):
    p = await shop(client, admin_headers, category, price=30000)
    await mk(client, admin_headers, "CAPPED", percent_off="50", max_discount_cents=2000)
    await mk(client, admin_headers, "HUGE", kind="fixed", percent_off=None, amount_off_cents=999999)
    await mk(client, admin_headers, "SHIP0", kind="free_shipping", percent_off=None)
    headers, _ = await signup(client)
    results = {}
    for code in ("CAPPED", "HUGE", "SHIP0"):
        await fill(client, headers, p["id"])
        o = (await place(client, headers, code)).json()
        results[code] = (o["discount_cents"], o["shipping_cents"])
    assert results == {"CAPPED": (2000, 5000), "HUGE": (30000, 5000), "SHIP0": (0, 0)}                                    # the goods can go to zero, never below


async def test_the_free_shipping_threshold_counts_what_the_customer_pays_for_the_goods(client, admin_headers, category, monkeypatch):
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_cents", 100000)
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_basis", "subtotal")
    p = await shop(client, admin_headers, category, price=100000)
    await mk(client, admin_headers, "TENOFF")
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    plain = (await place(client, headers)).json()
    assert plain["shipping_cents"] == 0                                                                                   # Rs 1,000 reaches the threshold
    await fill(client, headers, p["id"])
    discounted = (await place(client, headers, "TENOFF")).json()
    assert discounted["subtotal_cents"] == 100000 and discounted["discount_cents"] == 10000 and discounted["shipping_cents"] == 5000   # 900 does not


async def test_a_coupon_that_cannot_be_used_refuses_the_whole_checkout_and_changes_nothing(client, admin_headers, category, db_session):
    p = await shop(client, admin_headers, category, stock=5)
    headers, _ = await signup(client)
    await fill(client, headers, p["id"], 2)
    r = await place(client, headers, "NOSUCH")
    assert r.status_code == 422 and r.json()["code"] == "coupon_invalid"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT stock FROM products WHERE id = :p"), {"p": p["id"]})).scalar_one() == 5
    assert (await db_session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0
    assert len((await client.get(f"{API}/cart", headers=headers)).json()["items"]) == 1                                  # the cart is intact
    assert (await place(client, headers)).status_code == 201                                                              # and buying without it works


async def test_a_retried_checkout_with_the_same_key_and_coupon_is_the_same_order_and_a_different_coupon_is_refused(client, admin_headers, category):
    p = await shop(client, admin_headers, category)
    await mk(client, admin_headers, "TENOFF")
    await mk(client, admin_headers, "FIVEOFF", percent_off="5")
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    first = await place(client, headers, "TENOFF", key="K1")
    retry = await place(client, headers, "tenoff", key="K1")                                                              # same code, another letter case
    assert first.status_code == 201 and retry.status_code in (200, 201) and retry.json()["id"] == first.json()["id"]
    for other in ("FIVEOFF", None):
        clash = await place(client, headers, other, key="K1")
        assert clash.status_code == 409 and clash.json()["code"] == "idempotency_key_reused"


# ============================================================================================== limits
async def test_the_last_use_goes_to_exactly_one_of_several_simultaneous_buyers(client, admin_headers, category, db_session):
    p = await shop(client, admin_headers, category)
    await mk(client, admin_headers, "LASTONE", max_uses_total=1)
    buyers = []
    for _ in range(4):
        h, _ = await signup(client)
        await fill(client, h, p["id"])
        buyers.append(h)
    results = await asyncio.gather(*[place(client, h, "LASTONE") for h in buyers])
    assert sorted(r.status_code for r in results) == [201, 422, 422, 422]
    assert {r.json()["code"] for r in results if r.status_code == 422} == {"coupon_limit_reached"}
    assert (await db_session.execute(text("SELECT count(*) FROM orders WHERE coupon_id IS NOT NULL"))).scalar_one() == 1


async def test_a_cancelled_order_gives_its_coupon_use_back(client, admin_headers, category):
    p = await shop(client, admin_headers, category)
    await mk(client, admin_headers, "ONEPER", max_uses_per_customer=1)
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    first = (await place(client, headers, "ONEPER")).json()
    await fill(client, headers, p["id"])
    refused = await place(client, headers, "ONEPER")
    assert refused.status_code == 422 and refused.json()["code"] == "coupon_limit_reached"                                  # the first order is alive
    assert (await client.post(f"{API}/orders/{first['id']}/cancel", headers=headers)).status_code == 200
    assert (await place(client, headers, "ONEPER")).status_code == 201                                                      # its use was returned


async def test_a_refunded_order_also_gives_the_use_back(client, admin_headers, category, provider, test_session_factory):
    p = await shop(client, admin_headers, category)
    await mk(client, admin_headers, "ONEPER2", max_uses_per_customer=1)
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    order = (await place(client, headers, "ONEPER2")).json()
    await pay(client, headers, order["id"])
    await hook(client, provider.last_pi)
    assert (await client.post(f"{API}/admin/orders/{order['id']}/cancel-and-refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    await fill(client, headers, p["id"])
    assert (await place(client, headers, "ONEPER2")).status_code == 201


# ============================================================================================== tax, invoices and reports
async def test_gst_is_worked_out_on_the_price_after_the_discount_and_the_invoice_shows_it(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    async with test_session_factory() as session:
        await session.execute(text("TRUNCATE store_settings"))
        await session.commit()
    assert (await client.put(f"{API}/store/currency", json={"currency": "inr"}, headers=admin_headers)).status_code == 200
    await configure(client, admin_headers, admin2)
    p = await eighteen(client, admin_headers, admin2, category, "GSTCPN", 100000)
    await set_profile(client, admin_headers)
    await mk(client, admin_headers, "TENOFF")
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    o = (await place(client, headers, "TENOFF")).json()
    assert (o["discount_cents"], o["tax_cents"], o["total_cents"]) == (10000, 16200, 90000 + 5000 + 16200)                    # 18% of 900, not of 1,000
    line = o["tax_snapshot"]["lines"][0]
    assert (line["unit_price_paise"], line["discount_paise"], line["value_paise"], line["taxable_paise"], line["cgst_paise"]) == (100000, 10000, 90000, 90000, 8100)
    await pay(client, headers, o["id"])
    await hook(client, provider.last_pi)
    assert (await fulfil(client, admin_headers, o["id"])).status_code == 200
    d = (await client.get(f"{API}/orders/{o['id']}/invoice", headers=headers)).json()["document"]
    assert d["totals"]["goods_paise"] == 100000 and d["totals"]["discount_paise"] == 10000 and d["totals"]["taxable_paise"] == 90000
    assert d["totals"]["grand_total_paise"] == o["total_cents"] and d["coupon"] == {"code": "TENOFF"} and d["lines"][0]["discount_paise"] == 10000
    html = (await client.get(f"{API}/orders/{o['id']}/invoice/print", headers=headers)).text
    assert "coupon TENOFF" in html and "Goods before discount" in html


async def test_reports_count_goods_after_discounts_and_show_the_discounts(client, admin_headers, category, provider):
    p = await shop(client, admin_headers, category, price=100000)
    await mk(client, admin_headers, "TENOFF")
    for coupon in ("TENOFF", None):
        headers, _ = await signup(client)
        await fill(client, headers, p["id"], 2)
        order = (await place(client, headers, coupon)).json()
        await pay(client, headers, order["id"])
        await hook(client, provider.last_pi)
    summary = (await client.get(f"{API}/admin/reports/summary", headers=admin_headers)).json()
    assert summary["goods_cents"] == 2 * 200000 - 20000 and summary["discount_cents"] == 20000
    assert summary["gross_cents"] == summary["goods_cents"] + summary["shipping_cents"] + summary["tax_cents"]               # the books still balance
    top = (await client.get(f"{API}/admin/reports/top-products", headers=admin_headers)).json()["items"]
    assert [i["revenue_cents"] for i in top if i["product_id"] == p["id"]] == [380000]
    day = [d for d in (await client.get(f"{API}/admin/reports/sales-by-day", headers=admin_headers)).json()["days"] if d["orders"]][0]
    assert day["discount_cents"] == 20000 and day["goods_cents"] == 380000


async def test_an_orders_discount_is_frozen_in_the_database(client, admin_headers, category, db_session):
    p = await shop(client, admin_headers, category)
    await mk(client, admin_headers, "TENOFF")
    headers, _ = await signup(client)
    await fill(client, headers, p["id"])
    o = (await place(client, headers, "TENOFF")).json()
    for statement in ("UPDATE orders SET discount_cents = 0", "UPDATE orders SET coupon_code = 'OTHER1'", "UPDATE orders SET coupon_id = NULL, coupon_code = NULL",
                      "UPDATE order_items SET discount_cents = 0"):
        with pytest.raises(Exception, match="cannot be changed|violates check"):
            await db_session.execute(text(statement))
        await db_session.rollback()
    await db_session.execute(text("UPDATE orders SET status = status WHERE id = :o"), {"o": o["id"]})                          # other updates are untouched
    await db_session.commit()
