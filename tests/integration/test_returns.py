# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""Customer returns: eligibility, requests and quantity accounting, the administrator's handling, restocking, and the whole-order refund."""

import asyncio

from sqlalchemy import text
from test_cart_variants import add, signup
from test_invoices import fulfil
from test_order_payment_lifecycle import (  # noqa: F401
    category,  # noqa: F401
    hook,
    jobs,
    pay,
    provider,
)
from test_stock_ledger import assert_balanced
from test_variant_admin import make_product, tee
from test_variant_checkout import checkout

from app.core.config import get_settings

API = "/api/v1"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


async def shipped(client, admin_headers, provider, lines, *, ship=True):
    """A customer buys `lines` ([(kwargs for add, quantity)]), pays, and the order is shipped. Returns (headers, order)."""
    headers, _ = await signup(client)
    for body in lines:
        assert (await add(client, headers, **body)).status_code == 201
    order = (await checkout(client, headers, address=ADDR)).json()
    await pay(client, headers, order["id"])
    assert (await hook(client, provider.last_pi)).status_code == 200
    if ship:
        assert (await fulfil(client, admin_headers, order["id"])).status_code == 200
    return headers, order


async def stock_of(client, admin_headers, product_id):
    return (await client.get(f"{API}/products/{product_id}", headers=admin_headers)).json()["stock"]


def item_ids(order):
    return [i["id"] for i in order["items"]]


async def request(client, headers, order_id, items, comment=None):
    body = {"items": items, **({"comment": comment} if comment else {})}
    return await client.post(f"{API}/orders/{order_id}/returns", json=body, headers=headers)


def line(item_id, quantity=1, reason="damaged"):
    return {"order_item_id": item_id, "quantity": quantity, "reason": reason}


async def receive(client, admin_headers, return_id, entries):
    return await client.post(f"{API}/admin/returns/{return_id}/receive", json={"items": entries}, headers=admin_headers)


def got(item_id, received, restock=True, note=None):
    return {"order_item_id": item_id, "received_quantity": received, "restock": restock, **({"condition_note": note} if note else {})}


# ============================================================================================== eligibility
async def test_eligibility_says_whether_an_order_can_be_returned_and_how_many_of_each_item(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    paid_headers, paid = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 2}], ship=False)
    r = (await client.get(f"{API}/orders/{paid['id']}/returns/eligibility", headers=paid_headers)).json()
    assert r["eligible"] is False and r["reason_code"] == "order_not_shipped"                                         # not shipped yet
    assert (await fulfil(client, admin_headers, paid["id"])).status_code == 200
    ok = (await client.get(f"{API}/orders/{paid['id']}/returns/eligibility", headers=paid_headers)).json()
    assert ok["eligible"] is True and ok["window_days"] == 7 and ok["window_ends_at"]
    assert [(i["purchased"], i["returnable"]) for i in ok["items"]] == [(2, 2)]
    stranger, _ = await signup(client)
    assert (await client.get(f"{API}/orders/{paid['id']}/returns/eligibility", headers=stranger)).status_code == 404
    client.cookies.clear()
    assert (await client.get(f"{API}/orders/{paid['id']}/returns/eligibility")).status_code in (401, 403)
    await db_session.execute(text("UPDATE order_status_history SET created_at = now() - interval '9 days' WHERE order_id = :o AND lower(to_status) = 'fulfilled'"), {"o": paid["id"]})
    await db_session.commit()
    late = (await client.get(f"{API}/orders/{paid['id']}/returns/eligibility", headers=paid_headers)).json()
    assert late["eligible"] is False and late["reason_code"] == "return_window_closed" and "7-day" in late["reason"]
    # the window runs from DELIVERY when the store recorded it
    shipment = await client.post(f"{API}/orders/{paid['id']}/shipments", json={"carrier_name": "BlueDart"}, headers=admin_headers)
    assert shipment.status_code == 201
    await db_session.execute(text("UPDATE order_shipments SET status = 'delivered', delivered_at = now() WHERE order_id = :o"), {"o": paid["id"]})
    await db_session.commit()
    assert (await client.get(f"{API}/orders/{paid['id']}/returns/eligibility", headers=paid_headers)).json()["eligible"] is True


async def test_returns_can_be_switched_off(client, admin_headers, category, provider, monkeypatch):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 1}])
    monkeypatch.setattr(get_settings(), "return_window_days", 0)
    e = (await client.get(f"{API}/orders/{order['id']}/returns/eligibility", headers=headers)).json()
    assert e["eligible"] is False and e["reason_code"] == "returns_disabled"
    refused = await request(client, headers, order["id"], [line(item_ids(order)[0])])
    assert refused.status_code == 422 and refused.json()["code"] == "returns_disabled"


# ============================================================================================== requesting
async def test_a_request_is_validated_and_quantities_never_exceed_what_was_bought(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    other = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 3}])
    _h2, foreign = await shipped(client, admin_headers, provider, [{"product_id": other["id"], "quantity": 1}])
    pid = item_ids(order)[0]
    for bad in ([], [line(pid, 0)], [line(pid, 1, "because")], [line(999999)], [line(item_ids(foreign)[0])], [line(pid), line(pid)], [{"order_item_id": pid}]):
        assert (await client.post(f"{API}/orders/{order['id']}/returns", json={"items": bad}, headers=headers)).status_code in (422,), bad
    too_many = await request(client, headers, order["id"], [line(pid, 4)])
    assert too_many.status_code == 422 and too_many.json()["code"] == "return_quantity_exceeded" and "at most 3" in too_many.json()["detail"]
    first = await request(client, headers, order["id"], [line(pid, 2, "size_or_fit")], comment="  Too\x07 small  ")
    assert first.status_code == 201
    body = first.json()
    assert body["status"] == "requested" and body["customer_comment"] == "Too small" and body["items"][0]["reason"] == "size_or_fit"
    assert body["admin_note"] is None and body["user_id"] is None                                                     # internal fields stay internal
    more = await request(client, headers, order["id"], [line(pid, 2)])                                                # only 1 is left
    assert more.status_code == 422 and "at most 1" in more.json()["detail"]
    assert (await request(client, headers, order["id"], [line(pid, 1)])).status_code == 201
    none_left = await request(client, headers, order["id"], [line(pid, 1)])
    assert none_left.status_code == 422 and none_left.json()["code"] == "nothing_returnable"
    # a rejected or cancelled request gives its units back
    assert (await client.post(f"{API}/admin/returns/{body['id']}/reject", json={"reason": "Not eligible"}, headers=admin_headers)).status_code == 200
    again = await request(client, headers, order["id"], [line(pid, 2)])
    assert again.status_code == 201
    assert (await client.post(f"{API}/returns/{again.json()['id']}/cancel", headers=headers)).status_code == 200
    assert (await request(client, headers, order["id"], [line(pid, 2)])).status_code == 201


async def test_simultaneous_requests_for_the_last_unit_let_exactly_one_through(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 1}])
    results = await asyncio.gather(*[request(client, headers, order["id"], [line(item_ids(order)[0])]) for _ in range(4)])
    assert sorted(r.status_code for r in results) == [201, 422, 422, 422]
    assert (await db_session.execute(text("SELECT count(*) FROM return_requests"))).scalar_one() == 1


async def test_customers_see_only_their_own_returns_and_can_withdraw_until_the_goods_arrive(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 3}])
    pid = item_ids(order)[0]
    a = (await request(client, headers, order["id"], [line(pid)])).json()
    stranger, _ = await signup(client)
    assert (await client.get(f"{API}/returns/{a['id']}", headers=stranger)).status_code == 404
    assert (await client.post(f"{API}/returns/{a['id']}/cancel", headers=stranger)).status_code == 404
    assert (await client.get(f"{API}/returns/{a['id']}", headers=headers)).json()["id"] == a["id"]
    assert (await client.get(f"{API}/returns", headers=stranger)).json()["total"] == 0
    mine = (await client.get(f"{API}/returns", headers=headers)).json()
    assert mine["total"] == 1 and (await client.get(f"{API}/orders/{order['id']}/returns", headers=headers)).json()["total"] == 1
    assert (await client.post(f"{API}/returns/{a['id']}/cancel", headers=headers)).json()["status"] == "cancelled"
    assert (await client.post(f"{API}/returns/{a['id']}/cancel", headers=headers)).status_code == 409                # already withdrawn
    b = (await request(client, headers, order["id"], [line(pid)])).json()
    assert (await client.post(f"{API}/admin/returns/{b['id']}/approve", headers=admin_headers)).status_code == 200
    assert (await client.post(f"{API}/returns/{b['id']}/cancel", headers=headers)).status_code == 200               # approved but not received: fine
    c = (await request(client, headers, order["id"], [line(pid)])).json()
    await client.post(f"{API}/admin/returns/{c['id']}/approve", headers=admin_headers)
    await receive(client, admin_headers, c["id"], [got(pid, 1, restock=False)])
    late = await client.post(f"{API}/returns/{c['id']}/cancel", headers=headers)
    assert late.status_code == 409 and late.json()["code"] == "return_invalid_state"                                 # the goods have arrived


# ============================================================================================== the administrator
async def test_only_administrators_decide_and_the_states_follow_the_rules(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 3}])
    pid = item_ids(order)[0]
    ret = (await request(client, headers, order["id"], [line(pid)])).json()
    base = f"{API}/admin/returns/{ret['id']}"
    for path, body in (("approve", None), ("reject", {"reason": "no"}), ("receive", {"items": [got(pid, 1)]}), ("refund", None), ("close", {"resolution": "no_refund", "note": "ok ok"})):
        r = await client.post(f"{base}/{path}", json=body, headers=headers) if body is not None else await client.post(f"{base}/{path}", headers=headers)
        assert r.status_code == 403, path
    assert (await client.get(f"{API}/admin/returns", headers=headers)).status_code == 403
    assert (await client.post(f"{base}/reject", json={"reason": "no"}, headers=admin_headers)).status_code == 422             # a reason of 3+ characters
    assert (await client.post(f"{base}/receive", json={"items": [got(pid, 1)]}, headers=admin_headers)).status_code == 409    # not approved yet
    assert (await client.post(f"{base}/close", json={"resolution": "refunded", "note": "x y z"}, headers=admin_headers)).status_code == 422   # not a closing resolution
    approved = (await client.post(f"{base}/approve", json={"note": "Looks fine"}, headers=admin_headers)).json()
    assert approved["status"] == "approved" and approved["admin_note"] == "Looks fine"
    assert (await client.post(f"{base}/approve", headers=admin_headers)).status_code == 409                                   # already decided
    assert (await client.post(f"{base}/reject", json={"reason": "Too late"}, headers=admin_headers)).status_code == 409
    seen_by_customer = (await client.get(f"{API}/returns/{ret['id']}", headers=headers)).json()
    assert seen_by_customer["status"] == "approved" and seen_by_customer["admin_note"] is None                               # internal notes stay internal
    rejected = (await request(client, headers, order["id"], [line(pid)])).json()
    done = (await client.post(f"{API}/admin/returns/{rejected['id']}/reject", json={"reason": "Worn items are not accepted"}, headers=admin_headers)).json()
    assert done["status"] == "rejected" and done["rejection_reason"] == "Worn items are not accepted"
    assert (await client.get(f"{API}/returns/{rejected['id']}", headers=headers)).json()["rejection_reason"] == "Worn items are not accepted"   # the customer sees why


async def test_received_goods_go_back_in_stock_only_when_the_administrator_says_so_and_only_once(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 4}])
    pid = item_ids(order)[0]
    assert await stock_of(client, admin_headers, p["id"]) == 6
    ret = (await request(client, headers, order["id"], [line(pid, 3)])).json()
    base = f"{API}/admin/returns/{ret['id']}"
    await client.post(f"{base}/approve", headers=admin_headers)
    for bad in ([], [got(pid, 4)], [got(pid, 0, restock=True)], [got(pid, 1), got(pid, 1)], [got(999999, 1)]):
        assert (await receive(client, admin_headers, ret["id"], bad)).status_code in (422,), bad
    assert await stock_of(client, admin_headers, p["id"]) == 6                                                              # nothing moved
    done = await receive(client, admin_headers, ret["id"], [got(pid, 2, restock=True, note="Box opened, item fine")])      # 3 requested, 2 arrived
    assert done.status_code == 200
    r = done.json()
    assert r["status"] == "received" and r["items"][0]["received_quantity"] == 2 and r["items"][0]["restocked_quantity"] == 2
    assert r["items"][0]["condition_note"] == "Box opened, item fine"
    assert await stock_of(client, admin_headers, p["id"]) == 8
    assert (await receive(client, admin_headers, ret["id"], [got(pid, 2)])).status_code == 409                              # not twice
    assert await stock_of(client, admin_headers, p["id"]) == 8
    moves = (await db_session.execute(text("SELECT kind, delta, order_id::text, reason FROM stock_movements WHERE kind = 'restock'"))).all()
    assert [(m[0], m[1], m[2]) for m in moves] == [("restock", 2, order["id"])] and moves[0][3] == f"customer return {ret['id']}"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT restocked_quantity FROM order_items WHERE id = :i"), {"i": pid})).scalar_one() == 2
    await assert_balanced(db_session)
    keep = (await request(client, headers, order["id"], [line(pid, 1)])).json()                                              # the 4th unit
    await client.post(f"{API}/admin/returns/{keep['id']}/approve", headers=admin_headers)
    assert (await receive(client, admin_headers, keep["id"], [got(pid, 1, restock=False, note="Damaged")])).status_code == 200
    assert await stock_of(client, admin_headers, p["id"]) == 8                                                              # damaged goods stay out of stock


async def test_a_returned_variant_goes_back_to_that_variant(client, admin_headers, category, provider):
    detail = await tee(client, admin_headers, category, status="active", stock_each=6)
    variant = next(v for v in detail["variants"] if v["sku"] == "T-M-BLU")
    headers, order = await shipped(client, admin_headers, provider, [{"variant_id": variant["id"], "quantity": 2}])
    pid = item_ids(order)[0]
    ret = (await request(client, headers, order["id"], [line(pid, 2, "wrong_item")])).json()
    assert ret["items"][0]["options"] == [{"name": "Size", "value": "M"}, {"name": "Colour", "value": "Blue"}] and ret["items"][0]["sku"] == "T-M-BLU"
    await client.post(f"{API}/admin/returns/{ret['id']}/approve", headers=admin_headers)
    assert (await receive(client, admin_headers, ret["id"], [got(pid, 2)])).status_code == 200
    from sqlalchemy import text as _t  # noqa: F401
    assert (await client.get(f"{API}/products/{detail['id']}", headers=admin_headers)).json()["variants"][3]["stock"] == 6   # back to 6 (it was 4 after the sale)


# ============================================================================================== money
async def test_a_return_of_the_whole_order_can_start_its_refund_and_goods_are_not_restocked_twice(client, admin_headers, category, provider, test_session_factory, db_session):
    a = await make_product(client, admin_headers, category, stock=10, status="active")
    b = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": a["id"], "quantity": 2}, {"product_id": b["id"], "quantity": 1}])
    ia, ib = item_ids(order)
    first = (await request(client, headers, order["id"], [line(ia, 2)])).json()
    await client.post(f"{API}/admin/returns/{first['id']}/approve", headers=admin_headers)
    await receive(client, admin_headers, first["id"], [got(ia, 2)])
    not_full = await client.post(f"{API}/admin/returns/{first['id']}/refund", headers=admin_headers)                         # item b is still with the customer
    assert not_full.status_code == 409 and not_full.json()["code"] == "return_not_full"
    second = (await request(client, headers, order["id"], [line(ib, 1)])).json()
    await client.post(f"{API}/admin/returns/{second['id']}/approve", headers=admin_headers)
    await receive(client, admin_headers, second["id"], [got(ib, 1)])
    refund = await client.post(f"{API}/admin/returns/{second['id']}/refund", headers=admin_headers)                          # together they cover everything
    assert refund.status_code == 200, refund.text
    body = refund.json()
    assert body["status"] == "closed" and body["resolution"] == "refunded" and body["refund"]["id"]
    assert (await client.post(f"{API}/admin/returns/{second['id']}/refund", headers=admin_headers)).status_code == 409        # not twice
    await jobs(test_session_factory)                                                                                          # the provider confirms
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "refunded"
    assert (await client.get(f"{API}/returns/{second['id']}", headers=headers)).json()["refund"]["status"] in ("succeeded", "pending", "requested")
    assert await stock_of(client, admin_headers, a["id"]) == 10 and await stock_of(client, admin_headers, b["id"]) == 10      # restocked once, at receipt
    await assert_balanced(db_session)


async def test_a_partial_return_cannot_start_a_refund_but_is_closed_with_a_recorded_resolution(client, admin_headers, category, provider):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 2}])
    pid = item_ids(order)[0]
    ret = (await request(client, headers, order["id"], [line(pid, 1)])).json()
    await client.post(f"{API}/admin/returns/{ret['id']}/approve", headers=admin_headers)
    await receive(client, admin_headers, ret["id"], [got(pid, 1)])
    assert (await client.post(f"{API}/admin/returns/{ret['id']}/refund", headers=admin_headers)).json()["code"] == "return_not_full"
    closed = await client.post(f"{API}/admin/returns/{ret['id']}/close", json={"resolution": "refunded_outside", "note": "Refunded Rs 1,000 in the Stripe dashboard"}, headers=admin_headers)
    assert closed.status_code == 200
    c = closed.json()
    assert c["status"] == "closed" and c["resolution"] == "refunded_outside" and c["resolution_note"].startswith("Refunded Rs 1,000") and c["refund"] is None
    assert (await client.get(f"{API}/returns/{ret['id']}", headers=headers)).json()["resolution_note"] is None               # the note is internal
    assert (await client.post(f"{API}/admin/returns/{ret['id']}/close", json={"resolution": "no_refund", "note": "again"}, headers=admin_headers)).status_code == 409
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "fulfilled"               # the order itself is untouched
    never = (await request(client, headers, order["id"], [line(pid, 1)])).json()
    await client.post(f"{API}/admin/returns/{never['id']}/approve", headers=admin_headers)
    assert (await client.post(f"{API}/admin/returns/{never['id']}/close", json={"resolution": "no_refund", "note": "Goods never arrived"}, headers=admin_headers)).status_code == 200   # closed without receipt


async def test_the_administrators_list_filters_and_every_step_is_audited(client, admin_headers, category, provider, db_session):
    p = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, order = await shipped(client, admin_headers, provider, [{"product_id": p["id"], "quantity": 3}])
    pid = item_ids(order)[0]
    r1 = (await request(client, headers, order["id"], [line(pid)])).json()
    r2 = (await request(client, headers, order["id"], [line(pid)])).json()
    await client.post(f"{API}/admin/returns/{r1['id']}/approve", headers=admin_headers)
    await client.post(f"{API}/admin/returns/{r2['id']}/reject", json={"reason": "Not accepted"}, headers=admin_headers)
    everything = (await client.get(f"{API}/admin/returns", headers=admin_headers)).json()
    assert everything["total"] == 2 and everything["items"][0]["user_id"]
    assert [i["id"] for i in (await client.get(f"{API}/admin/returns", params={"status": "approved"}, headers=admin_headers)).json()["items"]] == [r1["id"]]
    assert (await client.get(f"{API}/admin/returns", params={"order_id": order["id"], "page_size": 1}, headers=admin_headers)).json()["total"] == 2
    assert (await client.get(f"{API}/admin/returns", params={"from_date": "2999-01-01"}, headers=admin_headers)).json()["total"] == 0
    assert (await client.get(f"{API}/admin/returns", params={"status": "bogus"}, headers=admin_headers)).status_code == 422
    assert (await client.get(f"{API}/admin/returns/{r1['id']}", headers=admin_headers)).json()["admin_note"] is None
    actions = {r[0] for r in (await db_session.execute(text("SELECT action FROM audit_logs"))).all()}
    assert {"return.requested", "return.approved", "return.rejected"} <= actions
