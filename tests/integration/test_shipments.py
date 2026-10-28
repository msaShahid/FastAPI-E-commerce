# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""Gap D: shipments (carrier, tracking number and link entered by an administrator), customer visibility, and the delivery-eligibility seam."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_order_payment_lifecycle import (  # noqa: F401
    ADDR,
    API,
    category,
    hook,
    jobs,
    make_order,
    one,
    paid_order,
    pay,
    provider,
)

from app.core.config import get_settings
from app.core.rate_limit import limiter

URL = "https://track.example-courier.com/p/DEL123"


def sh(order_id):
    return f"{API}/orders/{order_id}/shipments"


async def shipments_of(client, headers, order_id):
    return (await client.get(f"{API}/orders/{order_id}", headers=headers)).json()["shipments"]


# ================================================================================================== recording
async def test_only_administrators_can_record_change_or_delete_shipments(client, admin_headers, category, provider):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    body = {"carrier_name": "Delhivery"}
    assert (await client.post(sh(o["id"]), json=body)).status_code in (401, 403)
    assert (await client.post(sh(o["id"]), json=body, headers=h)).status_code == 403  # not even the order's own customer
    s = (await client.post(sh(o["id"]), json=body, headers=admin_headers)).json()
    assert (await client.patch(f"{sh(o['id'])}/{s['id']}", json={"status": "delivered"}, headers=h)).status_code == 403
    assert (await client.delete(f"{sh(o['id'])}/{s['id']}", headers=h)).status_code == 403
    assert (await client.delete(f"{sh(o['id'])}/{s['id']}", headers=admin_headers)).status_code == 204


async def test_a_shipment_needs_only_a_carrier_and_leaves_the_order_status_alone(client, admin_headers, category, provider):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    r = await client.post(sh(o["id"]), json={"carrier_name": "  Blue   Dart  "}, headers=admin_headers)
    assert r.status_code == 201, r.text
    s = r.json()
    assert (s["carrier_name"], s["tracking_number"], s["tracking_url"], s["status"]) == ("Blue Dart", None, None, "shipped")
    assert s["shipped_at"] and s["delivered_at"] is None
    seen = (await client.get(f"{API}/orders/{o['id']}", headers=h)).json()
    assert seen["status"] == "paid" and seen["shipments"] == [s]  # recording a parcel is not fulfilling the order


async def test_tracking_can_be_added_changed_and_cleared_later(client, admin_headers, category, provider):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    s = (await client.post(sh(o["id"]), json={"carrier_name": "DTDC"}, headers=admin_headers)).json()
    url = f"{sh(o['id'])}/{s['id']}"
    r = await client.patch(url, json={"tracking_number": "D12345678", "tracking_url": URL}, headers=admin_headers)
    assert r.status_code == 200 and (r.json()["tracking_number"], r.json()["tracking_url"]) == ("D12345678", URL)
    assert (await shipments_of(client, h, o["id"]))[0]["tracking_url"] == URL  # the customer sees it
    r = await client.patch(url, json={"carrier_name": "Delhivery"}, headers=admin_headers)  # a field left out is kept
    assert r.json()["carrier_name"] == "Delhivery" and r.json()["tracking_number"] == "D12345678"
    r = await client.patch(url, json={"tracking_url": None}, headers=admin_headers)  # null clears
    assert r.json()["tracking_url"] is None and r.json()["tracking_number"] == "D12345678"
    assert (await client.patch(url, json={"carrier_name": None}, headers=admin_headers)).status_code == 422


@pytest.mark.parametrize("bad", [
    {"tracking_url": "http://courier.com/t"}, {"tracking_url": "javascript:alert(1)"}, {"tracking_url": "https://127.0.0.1/t"},
    {"tracking_url": "https://u:p@courier.com/t"}, {"tracking_number": "has space"}, {"carrier_name": "<b>x</b>"},
    {"order_id": "00000000-0000-0000-0000-000000000000"}, {"status": "delivered"}, {"shipped_at": "2020-01-01T00:00:00Z"},
])
async def test_invalid_or_unexpected_input_is_refused(client, admin_headers, category, provider, bad):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    r = await client.post(sh(o["id"]), json={"carrier_name": "Delhivery", **bad}, headers=admin_headers)
    assert r.status_code == 422 and (await shipments_of(client, h, o["id"])) == []


async def test_an_order_can_have_several_shipments_but_not_the_same_tracking_number_twice(client, admin_headers, category, provider):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    ok = [await client.post(sh(o["id"]), json=b, headers=admin_headers) for b in (
        {"carrier_name": "Delhivery", "tracking_number": "AAA111"}, {"carrier_name": "DTDC", "tracking_number": "AAA111"},
        {"carrier_name": "Delhivery"}, {"carrier_name": "Delhivery"})]
    assert [r.status_code for r in ok] == [201] * 4  # same number with another carrier, and several untracked parcels, are fine
    dup = await client.post(sh(o["id"]), json={"carrier_name": "Delhivery", "tracking_number": "AAA111"}, headers=admin_headers)
    assert dup.status_code == 409 and dup.json()["code"] == "duplicate_tracking_number"
    assert len(await shipments_of(client, h, o["id"])) == 4  # and a failed insert left nothing behind
    first = ok[0].json()
    clash = await client.patch(f"{sh(o['id'])}/{ok[2].json()['id']}", json={"tracking_number": "AAA111"}, headers=admin_headers)
    assert clash.status_code == 409 and first["tracking_number"] == "AAA111"


# ================================================================================================== which orders can ship
async def test_only_paid_or_fulfilled_orders_can_be_shipped(client, admin_headers, category, provider, test_session_factory):
    limiter.reset()
    _, pending, _ = await make_order(client, admin_headers, category)  # never paid
    r = await client.post(sh(pending["id"]), json={"carrier_name": "Delhivery"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "order_not_shippable"

    limiter.reset()
    h2, cancelled, _ = await make_order(client, admin_headers, category)
    assert (await client.post(f"{API}/orders/{cancelled['id']}/cancel", headers=h2)).status_code == 200
    assert (await client.post(sh(cancelled["id"]), json={"carrier_name": "Delhivery"}, headers=admin_headers)).status_code == 409

    limiter.reset()
    h3, refunded, _, _ = await paid_order(client, admin_headers, category, provider)
    assert (await client.post(f"{API}/payments/{refunded['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert (await client.post(sh(refunded["id"]), json={"carrier_name": "Delhivery"}, headers=admin_headers)).status_code == 409

    limiter.reset()
    h4, fulfilled, _, _ = await paid_order(client, admin_headers, category, provider)
    assert (await client.patch(f"{API}/orders/{fulfilled['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)).status_code == 200
    assert (await client.post(sh(fulfilled["id"]), json={"carrier_name": "Delhivery"}, headers=admin_headers)).status_code == 201  # a second parcel
    assert (await client.post(sh("00000000-0000-0000-0000-000000000000"), json={"carrier_name": "X1"}, headers=admin_headers)).status_code == 404


async def test_fulfil_order_moves_a_paid_order_through_the_normal_status_rules_and_records_history(client, admin_headers, category, provider, db_session):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    r = await client.post(sh(o["id"]), json={"carrier_name": "Delhivery", "tracking_number": "FUL123", "fulfill_order": True}, headers=admin_headers)
    assert r.status_code == 201
    assert (await client.get(f"{API}/orders/{o['id']}", headers=h)).json()["status"] == "fulfilled"
    await db_session.rollback()
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND upper(to_status) = 'FULFILLED'", o=o["id"]) == 1
    # on an order that is already fulfilled the flag changes nothing and does not fail
    again = await client.post(sh(o["id"]), json={"carrier_name": "DTDC", "fulfill_order": True}, headers=admin_headers)
    assert again.status_code == 201
    await db_session.rollback()
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND upper(to_status) = 'FULFILLED'", o=o["id"]) == 1


async def test_a_shipment_never_changes_what_the_customer_was_charged(client, admin_headers, category, provider):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    await client.post(sh(o["id"]), json={"carrier_name": "Delhivery", "fulfill_order": True}, headers=admin_headers)
    after = (await client.get(f"{API}/orders/{o['id']}", headers=h)).json()
    for key in ("subtotal_cents", "shipping_cents", "tax_cents", "total_cents", "currency"):
        assert after[key] == o[key]


# ================================================================================================== delivery status
async def test_a_shipment_can_be_marked_delivered_once_and_not_taken_back(client, admin_headers, category, provider, db_session):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    s = (await client.post(sh(o["id"]), json={"carrier_name": "Delhivery"}, headers=admin_headers)).json()
    url = f"{sh(o['id'])}/{s['id']}"
    done = (await client.patch(url, json={"status": "delivered"}, headers=admin_headers)).json()
    assert done["status"] == "delivered" and done["delivered_at"]
    again = await client.patch(url, json={"status": "delivered"}, headers=admin_headers)  # repeating it is harmless
    assert again.status_code == 200 and again.json()["delivered_at"] == done["delivered_at"]
    back = await client.patch(url, json={"status": "shipped"}, headers=admin_headers)
    assert back.status_code == 409 and back.json()["code"] == "invalid_shipment_status_change"
    assert (await client.patch(url, json={"status": "lost"}, headers=admin_headers)).status_code == 422
    await db_session.rollback()
    with pytest.raises(IntegrityError):  # the database itself refuses a delivered shipment without a delivery time
        await db_session.execute(text("UPDATE order_shipments SET delivered_at = NULL"))
    await db_session.rollback()


# ================================================================================================== who sees what
async def test_a_customer_sees_their_own_tracking_in_the_order_detail_and_the_list_and_nobody_elses(client, admin_headers, category, provider):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    limiter.reset()
    other, o2, _, _ = await paid_order(client, admin_headers, category, provider)
    await client.post(sh(o["id"]), json={"carrier_name": "Delhivery", "tracking_number": "MINE123", "tracking_url": URL}, headers=admin_headers)
    await client.post(sh(o2["id"]), json={"carrier_name": "DTDC", "tracking_number": "THEIRS456"}, headers=admin_headers)

    mine = (await client.get(f"{API}/orders/{o['id']}", headers=h)).json()["shipments"]
    assert [(s["carrier_name"], s["tracking_number"]) for s in mine] == [("Delhivery", "MINE123")]
    listed = (await client.get(f"{API}/orders", headers=h)).json()["items"]
    assert [[s["tracking_number"] for s in item["shipments"]] for item in listed] == [["MINE123"]]
    assert "THEIRS456" not in (await client.get(f"{API}/orders", headers=h)).text
    assert (await client.get(f"{API}/orders/{o2['id']}", headers=h)).status_code in (403, 404)
    assert "created_by" not in json.dumps(mine) and "order_id" not in json.dumps(mine)  # no internal fields leak


async def test_a_guest_sees_tracking_on_their_order(client, admin_headers, category, provider):
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": "G", "description": None, "price_cents": 1200, "sku": "GS-1", "stock": 5, "category_id": category, "status": "active"})).json()
    client.cookies.clear()
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})
    co = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR, "guest_email": "g@example.com"}, headers={"Idempotency-Key": "gs-1"})
    assert co.status_code == 201, co.text
    order = co.json()
    pay_cookie = next(c for c in co.headers.get_list("set-cookie") if c.startswith("guest_pay_")).split(";", 1)[0]
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers={"Cookie": pay_cookie})).status_code == 201
    assert (await hook(client, provider.last_pi)).status_code == 200
    await client.post(sh(order["id"]), json={"carrier_name": "India Post", "tracking_number": "EE123456789IN"}, headers=admin_headers)
    seen = (await client.get(f"{API}/orders/guest/{order['id']}")).json()  # the guest's own cart cookie is in the client's jar
    assert [(s["carrier_name"], s["tracking_number"]) for s in seen["shipments"]] == [("India Post", "EE123456789IN")]


async def test_deleting_a_shipment_removes_it_from_the_customers_view_and_is_audited_without_the_number(client, admin_headers, category, provider, db_session):
    h, o, _, _ = await paid_order(client, admin_headers, category, provider)
    s = (await client.post(sh(o["id"]), json={"carrier_name": "Delhivery", "tracking_number": "SECRETISH99"}, headers=admin_headers)).json()
    assert (await client.delete(f"{sh(o['id'])}/{s['id']}", headers=admin_headers)).status_code == 204
    assert await shipments_of(client, h, o["id"]) == []
    assert (await client.delete(f"{sh(o['id'])}/{s['id']}", headers=admin_headers)).status_code == 404
    await db_session.rollback()
    audit = (await db_session.execute(text("SELECT string_agg(action || ' ' || details::text, ' | ') FROM audit_logs WHERE action LIKE 'shipment.%'"))).scalar_one()
    assert "shipment.created" in audit and "shipment.deleted" in audit and "SECRETISH99" not in audit


async def test_a_shipment_of_one_order_cannot_be_reached_through_another(client, admin_headers, category, provider):
    h, a, _, _ = await paid_order(client, admin_headers, category, provider)
    limiter.reset()
    h2, b, _, _ = await paid_order(client, admin_headers, category, provider)
    s = (await client.post(sh(a["id"]), json={"carrier_name": "Delhivery"}, headers=admin_headers)).json()
    assert (await client.patch(f"{sh(b['id'])}/{s['id']}", json={"status": "delivered"}, headers=admin_headers)).status_code == 404
    assert (await client.delete(f"{sh(b['id'])}/{s['id']}", headers=admin_headers)).status_code == 404
    assert len(await shipments_of(client, h, a["id"])) == 1


# ================================================================================================== delivery eligibility seam
async def test_by_default_no_address_is_checked_and_each_order_records_that_fact(client, admin_headers, category):
    _, o, _ = await make_order(client, admin_headers, category)
    assert o["shipping_snapshot"]["delivery_check"] == {"checked": False, "policy": "none"}
    assert (await client.get(f"{API}/store/settings")).json()["delivery_check"] == "none"


class RejectsOnePincode:
    policy = "test_list"

    async def check(self, address):
        from app.modules.orders.services.eligibility import DeliveryDecision

        if address["shipping_postal_code"] == "999999":
            return DeliveryDecision(False, True, self.policy, "pincode_not_serviceable")
        return DeliveryDecision(True, True, self.policy)


async def test_a_future_policy_refuses_an_undeliverable_address_before_anything_is_changed(client, admin_headers, category, monkeypatch, db_session):
    from app.modules.orders.dependencies import order_deps

    monkeypatch.setattr(order_deps, "get_delivery_eligibility", lambda: RejectsOnePincode())
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": "E", "description": None, "price_cents": 1000, "sku": "EL-1", "stock": 5, "category_id": category, "status": "active"})).json()
    client.cookies.clear()
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 2})

    async def checkout(pincode, key):
        return await client.post(f"{API}/orders/checkout", headers={"Idempotency-Key": key},
                                 json={"shipping_address": {**ADDR, "postal_code": pincode}, "guest_email": "e@example.com"})

    refused = await checkout("999999", "el-1")
    assert refused.status_code == 422 and refused.json()["code"] == "delivery_not_available"
    await db_session.rollback()
    assert await one(db_session, "SELECT stock FROM products WHERE id = :p", p=p["id"]) == 5            # no stock was taken
    assert await one(db_session, "SELECT count(*) FROM orders") == 0                                      # no order exists
    assert (await client.get(f"{API}/cart")).json()["items"][0]["quantity"] == 2                          # the cart is intact
    accepted = await checkout("560001", "el-2")
    assert accepted.status_code == 201
    assert accepted.json()["shipping_snapshot"]["delivery_check"] == {"checked": True, "policy": "test_list"}


async def test_shipping_charges_are_unchanged_by_the_seam(client, admin_headers, category, monkeypatch):
    monkeypatch.setattr(get_settings(), "flat_shipping_cents", 4900)
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_cents", 99900)
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_basis", "subtotal")  # a threshold needs a stated basis
    _, small, _ = await make_order(client, admin_headers, category, price=1000)
    limiter.reset()
    _, big, _ = await make_order(client, admin_headers, category, price=100000)
    assert small["shipping_cents"] == 4900 and big["shipping_cents"] == 0
