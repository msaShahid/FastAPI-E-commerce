# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""E5: administering options and variants, stock adjustments and history, converting a product, and the variant-aware reports."""

import asyncio
import uuid

from sqlalchemy import text
from test_cache_integration import cache, redis_client  # noqa: F401
from test_cart_variants import add, signup
from test_order_payment_lifecycle import category, hook, jobs, pay, provider  # noqa: F401
from test_stock_ledger import assert_balanced
from test_variant_checkout import checkout, vstock

API = "/api/v1"
TEE = {"options": [{"name": "Size", "values": ["S", "M", "L"]}, {"name": "Colour", "values": ["Black", "Blue"]}]}


async def make_product(client, admin_headers, category, *, sku=None, stock=0, status="draft", price=1000, name=None):
    r = await client.post(f"{API}/products", headers=admin_headers, json={
        "name": name or f"Prod {uuid.uuid4().hex[:6]}", "description": None, "price_cents": price, "sku": sku or f"P-{uuid.uuid4().hex[:8]}",
        "stock": stock, "category_id": category, "status": status})
    assert r.status_code == 201, r.text
    return r.json()


def v(sku, size, colour="Black", stock=0, price=None, status="active"):
    return {"sku": sku, "option_values": {"Size": size, "Colour": colour}, "stock": stock, "price_override_cents": price, "status": status}


async def put_options(client, headers, pid, body=TEE):
    return await client.put(f"{API}/products/{pid}/options", json=body, headers=headers)


async def bulk(client, headers, pid, specs):
    return await client.post(f"{API}/products/{pid}/variants/bulk", json={"variants": specs}, headers=headers)


async def tee(client, admin_headers, category, *, status="draft", stock_each=5, prefix="T"):
    """A product with Size x Colour options and four of the six combinations created."""
    p = await make_product(client, admin_headers, category, status="draft")
    assert (await put_options(client, admin_headers, p["id"])).status_code == 200
    specs = [v(f"{prefix}-S-BLK", "S", stock=stock_each), v(f"{prefix}-M-BLK", "M", stock=stock_each, price=900), v(f"{prefix}-L-BLK", "L", stock=stock_each),
             v(f"{prefix}-M-BLU", "M", "Blue", stock=stock_each)]
    r = await bulk(client, admin_headers, p["id"], specs)
    assert r.status_code == 201, r.text
    if status == "active":
        assert (await client.patch(f"{API}/products/{p['id']}", headers=admin_headers, json={"status": "active"})).status_code == 200
    return r.json()


def variants_by_sku(detail):
    return {x["sku"]: x for x in detail["variants"]}


# ============================================================================================== access
async def test_only_administrators_can_use_the_variant_endpoints(client, admin_headers, category):
    p = await make_product(client, admin_headers, category)
    pid = p["id"]
    customer, _ = await signup(client)
    calls = [
        ("put", f"/products/{pid}/options", TEE), ("post", f"/products/{pid}/variants", v("X-1", "S")), ("post", f"/products/{pid}/variants/bulk", {"variants": []}),
        ("patch", f"/products/{pid}/variants/1", {"status": "inactive"}), ("delete", f"/products/{pid}/variants/1", None),
        ("post", f"/products/{pid}/variants/1/stock", {"delta": 1, "reason": "test"}), ("post", f"/products/{pid}/stock", {"delta": 1, "reason": "test"}),
        ("get", f"/products/{pid}/stock-history", None), ("get", f"/products/{pid}/variants/missing-combinations", None),
        ("post", f"/products/{pid}/convert-to-variants", {}), ("post", f"/products/{pid}/options/1/values", {"value": "XL"}),
    ]
    for method, path, body in calls:
        kw = {"json": body} if body is not None else {}
        assert (await getattr(client, method)(f"{API}{path}", **kw)).status_code in (401, 403), path
        assert (await getattr(client, method)(f"{API}{path}", headers=customer, **kw)).status_code == 403, path


# ============================================================================================== options and variants
async def test_options_then_chosen_variants_make_a_product_with_options_and_nothing_is_generated(client, admin_headers, category):
    p = await make_product(client, admin_headers, category)
    r = await put_options(client, admin_headers, p["id"])
    assert r.status_code == 200 and [(o["name"], o["values"]) for o in r.json()["options"]] == [("Size", ["S", "M", "L"]), ("Colour", ["Black", "Blue"])]
    assert r.json()["has_variants"] is True and r.json()["variants"] == []                       # options alone create no variants
    detail = await tee(client, admin_headers, category)
    assert len(detail["variants"]) == 4
    by_sku = variants_by_sku(detail)
    assert by_sku["T-M-BLK"]["price_cents"] == 900 and by_sku["T-M-BLK"]["price_overridden"] is True and by_sku["T-S-BLK"]["price_cents"] == 1000
    assert by_sku["T-M-BLU"]["options"] == [{"name": "Size", "value": "M"}, {"name": "Colour", "value": "Blue"}]
    assert detail["stock"] == 20 and detail["base_price_cents"] == 1000                           # administrators see exact stock

    pid = detail["id"]
    missing = (await client.get(f"{API}/products/{pid}/variants/missing-combinations", headers=admin_headers)).json()
    assert missing["total"] == 2 and not missing["truncated"]
    assert {tuple(sorted(m.items())) for m in missing["items"]} == {(("Colour", "Blue"), ("Size", "L")), (("Colour", "Blue"), ("Size", "S"))}
    again = (await client.get(f"{API}/products/{pid}/variants/missing-combinations", headers=admin_headers)).json()
    assert again["total"] == 2                                                                    # asking creates nothing

    assert (await client.patch(f"{API}/products/{pid}", headers=admin_headers, json={"status": "active"})).status_code == 200
    public = (await client.get(f"{API}/products/{pid}")).json()
    assert (public["price_from_cents"], public["price_to_cents"], public["stock"], public["availability"]) == (900, 1000, None, "low_stock")   # 5 units: at the threshold


async def test_bad_option_definitions_are_refused(client, admin_headers, category):
    p = await make_product(client, admin_headers, category)
    pid = p["id"]
    opt = lambda name, *values: {"name": name, "values": list(values)}  # noqa: E731
    bad = [
        {"options": [opt("A", "1"), opt("B", "1"), opt("C", "1"), opt("D", "1")]},     # at most 3
        {"options": [opt("Size", "S"), opt("size", "M")]},                            # the same name twice
        {"options": [opt("Size", "S", "s")]},                                         # the same value twice
        {"options": [opt("Size")]},                                                   # no values
        {"options": [opt("  ", "S")]}, {"options": [opt("Size", "S\x07")]},
        {"options": [opt("Size", "S")], "extra": 1},
    ]
    for body in bad:
        assert (await put_options(client, admin_headers, pid, body)).status_code == 422, body
    assert (await client.get(f"{API}/products/{pid}", headers=admin_headers)).json()["has_variants"] is False


async def test_a_product_in_use_is_not_given_options_directly_but_must_be_converted(client, admin_headers, category):
    stocked = await make_product(client, admin_headers, category, stock=3)
    r = await put_options(client, admin_headers, stocked["id"])
    assert r.status_code == 409 and r.json()["code"] == "product_has_stock"
    carted = await make_product(client, admin_headers, category, status="active", stock=0)
    headers, _ = await signup(client)
    await client.post(f"{API}/cart/items", json={"product_id": carted["id"], "quantity": 1}, headers=headers)   # stock 0: refused
    detail = await tee(client, admin_headers, category)
    r = await put_options(client, admin_headers, detail["id"], {"options": [{"name": "Fit", "values": ["Slim"]}]})
    assert r.status_code == 409 and r.json()["code"] == "options_locked"                         # variants exist: the structure is fixed
    fresh = await make_product(client, admin_headers, category, status="active", stock=2)
    await add(client, headers, product_id=fresh["id"], quantity=1)
    await client.patch(f"{API}/products/{fresh['id']}", headers=admin_headers, json={"stock": 0, "stock_reason": "sold elsewhere"})
    r = await put_options(client, admin_headers, fresh["id"])
    assert r.status_code == 409 and r.json()["code"] == "product_in_use"                         # a cart line references it
    assert (await put_options(client, admin_headers, (await make_product(client, admin_headers, category))["id"], {"options": []})).status_code == 200


async def test_variant_definitions_are_validated_and_conflicts_are_reported(client, admin_headers, category):
    p = await make_product(client, admin_headers, category, sku="STYLE-1")
    pid = p["id"]
    other = await make_product(client, admin_headers, category, sku="OTHER-1")
    assert (await bulk(client, admin_headers, pid, [v("A-1", "S")])).status_code == 422          # no options defined yet
    await put_options(client, admin_headers, pid)
    ok = await client.post(f"{API}/products/{pid}/variants", json=v("A-1", "S"), headers=admin_headers)
    assert ok.status_code == 201

    def one(spec):
        return client.post(f"{API}/products/{pid}/variants", json=spec, headers=admin_headers)

    assert (await one({"sku": "A-2", "option_values": {"Size": "S"}})).status_code == 422                    # Colour missing
    assert (await one({"sku": "A-2", "option_values": {"Size": "S", "Colour": "Green"}})).status_code == 422  # not a value
    assert (await one({"sku": "A-2", "option_values": {"Size": "S", "Colour": "Black", "Fit": "x"}})).status_code == 422
    dup = await one(v("A-9", "S"))
    assert dup.status_code == 409 and dup.json()["code"] == "variant_combination_exists"         # the combination exists already
    for clash in ("a-1", "STYLE-1", "style-1", "other-1"):                                        # another variant, own product, any product
        r = await one(v(clash, "M"))
        assert r.status_code == 409 and r.json()["code"] == "sku_already_exists", clash
    for bad in (v("bad sku", "M"), v("", "M"), v("X" * 65, "M"), v("A-3", "M", price=0), v("A-3", "M", stock=-1), {**v("A-3", "M"), "status": "gone"}, {**v("A-3", "M"), "sku_x": 1}):
        assert (await one(bad)).status_code == 422, bad
    twice = await bulk(client, admin_headers, pid, [v("B-1", "M"), v("B-2", "M")])               # one combination twice in a request
    assert twice.status_code == 422
    sku_twice = await bulk(client, admin_headers, pid, [v("B-1", "M"), v("b-1", "L")])
    assert sku_twice.status_code == 422
    all_or_none = await bulk(client, admin_headers, pid, [v("C-1", "M"), v("C-2", "L"), v("a-1", "M", "Blue")])   # the third clashes
    assert all_or_none.status_code == 409
    assert len((await client.get(f"{API}/products/{pid}", headers=admin_headers)).json()["variants"]) == 1      # nothing from the failed batch
    assert (await bulk(client, admin_headers, pid, [v(f"M-{i}", "M", f"C{i}") for i in range(3)])).status_code == 422   # unknown colours
    assert (await bulk(client, admin_headers, pid, [v(f"N-{i}", "M") for i in range(101)])).status_code == 422          # more than 100
    assert other["id"]


async def test_a_variants_price_and_status_can_change_but_not_its_identity(client, admin_headers, category):
    detail = await tee(client, admin_headers, category, status="active")
    pid, vs = detail["id"], variants_by_sku(detail)
    vid = vs["T-S-BLK"]["id"]

    async def patch(variant_id, body):
        return await client.patch(f"{API}/products/{pid}/variants/{variant_id}", json=body, headers=admin_headers)

    r = await patch(vid, {"price_override_cents": 750})
    assert variants_by_sku(r.json())["T-S-BLK"]["price_cents"] == 750 and r.json()["price_from_cents"] == 750
    r = await patch(vid, {"price_override_cents": None})                                         # null removes the override
    assert variants_by_sku(r.json())["T-S-BLK"]["price_overridden"] is False
    assert (await patch(vid, {"sku": "NEW"})).status_code == 422 and (await patch(vid, {"stock": 99})).status_code == 422
    assert (await patch(vid, {"option_values": {"Size": "L"}})).status_code == 422
    assert (await patch(vid, {"price_override_cents": 0})).status_code == 422
    assert (await patch(999999, {"status": "inactive"})).status_code == 404
    other = await tee(client, admin_headers, category, prefix="U")
    assert (await patch(other["variants"][0]["id"], {"status": "inactive"})).status_code == 404   # a variant of another product
    for sku in ("T-S-BLK", "T-M-BLK", "T-L-BLK"):
        assert (await patch(vs[sku]["id"], {"status": "inactive"})).status_code == 200
    last = await patch(vs["T-M-BLU"]["id"], {"status": "inactive"})                               # the product is active: it must keep one
    assert last.status_code == 409 and last.json()["code"] == "product_needs_active_variant"
    assert variants_by_sku((await client.get(f"{API}/products/{pid}", headers=admin_headers)).json())["T-M-BLU"]["status"] == "active"
    assert (await client.patch(f"{API}/products/{other['id']}", headers=admin_headers, json={"status": "active"})).status_code == 200
    results = [await client.patch(f"{API}/products/{other['id']}/variants/{x['id']}", json={"status": "inactive"}, headers=admin_headers) for x in other["variants"]]
    assert [r.status_code for r in results] == [200, 200, 200, 409] and results[-1].json()["code"] == "product_needs_active_variant"
    assert (await client.patch(f"{API}/products/{other['id']}", headers=admin_headers, json={"price_cents": 1100})).status_code == 200   # other edits still fine
    draft = await tee(client, admin_headers, category, prefix="V")
    for x in draft["variants"]:
        await client.patch(f"{API}/products/{draft['id']}/variants/{x['id']}", json={"status": "inactive"}, headers=admin_headers)   # a DRAFT may have none active
    activate = await client.patch(f"{API}/products/{draft['id']}", headers=admin_headers, json={"status": "active"})
    assert activate.status_code == 409 and activate.json()["code"] == "product_needs_active_variant"                                   # ... but cannot go live


async def test_option_values_can_be_added_renamed_and_removed_when_unused(client, admin_headers, category):
    detail = await tee(client, admin_headers, category, status="active")
    pid = detail["id"]
    size = next(o for o in detail["options"] if o["name"] == "Size")
    ids = dict(zip(size["values"], size["value_ids"], strict=True))

    r = await client.post(f"{API}/products/{pid}/options/{size['id']}/values", json={"value": "XL"}, headers=admin_headers)
    assert r.status_code == 201
    size = next(o for o in r.json()["options"] if o["name"] == "Size")
    assert size["values"] == ["S", "M", "L", "XL"]
    xl = size["value_ids"][-1]
    assert (await client.post(f"{API}/products/{pid}/options/{size['id']}/values", json={"value": "xl"}, headers=admin_headers)).status_code == 422
    assert (await client.post(f"{API}/products/{pid}/options/999999/values", json={"value": "Q"}, headers=admin_headers)).status_code == 404
    renamed = await client.patch(f"{API}/products/{pid}/option-values/{ids['S']}", json={"value": "Small"}, headers=admin_headers)
    assert renamed.status_code == 200 and variants_by_sku(renamed.json())["T-S-BLK"]["options"][0]["value"] == "Small"
    assert (await client.patch(f"{API}/products/{pid}/option-values/{ids['M']}", json={"value": "small"}, headers=admin_headers)).status_code == 422   # taken
    assert (await client.delete(f"{API}/products/{pid}/option-values/{ids['S']}", headers=admin_headers)).status_code == 409   # a variant uses it
    assert (await client.delete(f"{API}/products/{pid}/option-values/{xl}", headers=admin_headers)).status_code == 200


# ============================================================================================== stock
async def test_stock_changes_need_a_reason_are_recorded_and_the_history_is_readable(client, admin_headers, category, db_session):
    detail = await tee(client, admin_headers, category, status="active", stock_each=10)
    pid, vs = detail["id"], variants_by_sku(detail)
    vid = vs["T-S-BLK"]["id"]
    me = (await client.get(f"{API}/auth/me", headers=admin_headers)).json()["id"]
    url = f"{API}/products/{pid}/variants/{vid}/stock"

    absolute = await client.post(url, json={"set_to": 25, "reason": "annual recount"}, headers=admin_headers)
    assert absolute.json() == {"product_id": pid, "variant_id": vid, "before_qty": 10, "after_qty": 25, "delta": 15, "changed": True}
    relative = await client.post(url, json={"delta": -4, "reason": "damaged in storage"}, headers=admin_headers)
    assert (relative.json()["before_qty"], relative.json()["after_qty"]) == (25, 21)
    same = await client.post(url, json={"set_to": 21, "reason": "checked again"}, headers=admin_headers)
    assert same.json()["changed"] is False and same.json()["delta"] == 0                          # nothing happened, nothing recorded
    for bad in ({"set_to": 5}, {"set_to": 5, "reason": "ab"}, {"reason": "no amount"}, {"set_to": 5, "delta": 1, "reason": "both"}, {"delta": 0, "reason": "zero"}, {"set_to": -1, "reason": "negative"}):
        assert (await client.post(url, json=bad, headers=admin_headers)).status_code == 422, bad
    below = await client.post(url, json={"delta": -100, "reason": "too much"}, headers=admin_headers)
    assert below.status_code == 422 and below.json()["code"] == "stock_would_be_negative"
    assert await vstock(db_session, vid) == 21

    history = (await client.get(f"{API}/products/{pid}/stock-history", params={"variant_id": vid}, headers=admin_headers)).json()
    assert history["total"] == 3                                                                    # initial, set, delta
    newest = history["items"][0]
    assert (newest["kind"], newest["delta"], newest["before_qty"], newest["after_qty"], newest["reason"], newest["actor_user_id"]) == ("admin_delta", -4, 25, 21, "damaged in storage", me)
    assert [i["kind"] for i in history["items"]] == ["admin_delta", "admin_set", "initial"]
    page = (await client.get(f"{API}/products/{pid}/stock-history", params={"page_size": 2, "page": 2}, headers=admin_headers)).json()
    assert page["total"] >= 6 and len(page["items"]) == 2                                          # the product's four variants and the changes
    await assert_balanced(db_session)


async def test_a_simple_products_stock_has_its_own_endpoint_and_a_product_with_options_refuses_it(client, admin_headers, category, db_session):
    simple = await make_product(client, admin_headers, category, stock=5, status="active")
    r = await client.post(f"{API}/products/{simple['id']}/stock", json={"delta": 7, "reason": "delivery received"}, headers=admin_headers)
    assert (r.json()["before_qty"], r.json()["after_qty"]) == (5, 12)
    detail = await tee(client, admin_headers, category)
    refused = await client.post(f"{API}/products/{detail['id']}/stock", json={"delta": 1, "reason": "wrong place"}, headers=admin_headers)
    assert refused.status_code == 422 and refused.json()["code"] == "use_variant_stock"
    other = await tee(client, admin_headers, category, prefix="U")                                   # a variant of ANOTHER product
    wrong = await client.post(f"{API}/products/{detail['id']}/variants/{other['variants'][0]['id']}/stock", json={"delta": 1, "reason": "wrong product"}, headers=admin_headers)
    assert wrong.status_code == 404
    await assert_balanced(db_session)


async def test_simultaneous_stock_adjustments_keep_the_record_exact(client, admin_headers, category, db_session):
    detail = await tee(client, admin_headers, category, stock_each=0)
    pid, vid = detail["id"], detail["variants"][0]["id"]
    url = f"{API}/products/{pid}/variants/{vid}/stock"
    results = await asyncio.gather(*[client.post(url, json={"delta": 1, "reason": f"delivery {n}"}, headers=admin_headers) for n in range(8)])
    assert [r.status_code for r in results] == [200] * 8
    assert await vstock(db_session, vid) == 8
    rows = (await db_session.execute(text("SELECT before_qty, after_qty FROM stock_movements WHERE variant_id = :v ORDER BY id"), {"v": vid})).all()
    assert [r[1] for r in rows] == list(range(1, 9)) and [r[0] for r in rows] == list(range(0, 8))   # an unbroken chain: no two saw the same "before"
    await assert_balanced(db_session)


async def test_a_variant_can_be_deleted_only_while_it_has_no_history(client, admin_headers, category):
    detail = await tee(client, admin_headers, category, stock_each=0)                                 # created with no stock: no ledger rows
    pid = detail["id"]
    vs = variants_by_sku(detail)
    removable = vs["T-S-BLK"]["id"]
    assert (await client.delete(f"{API}/products/{pid}/variants/{removable}", headers=admin_headers)).status_code == 200
    stocked = vs["T-M-BLK"]["id"]
    await client.post(f"{API}/products/{pid}/variants/{stocked}/stock", json={"delta": 2, "reason": "delivery"}, headers=admin_headers)
    refused = await client.delete(f"{API}/products/{pid}/variants/{stocked}", headers=admin_headers)
    assert refused.status_code == 409 and refused.json()["code"] == "variant_in_use"                  # it has stock history: set it inactive instead
    headers, _ = await signup(client)
    assert (await client.patch(f"{API}/products/{pid}", headers=admin_headers, json={"status": "active"})).status_code == 200
    carted = vs["T-L-BLK"]["id"]
    await client.post(f"{API}/products/{pid}/variants/{carted}/stock", json={"delta": 1, "reason": "delivery"}, headers=admin_headers)
    await add(client, headers, variant_id=carted, quantity=1)
    assert (await client.delete(f"{API}/products/{pid}/variants/{carted}", headers=admin_headers)).status_code == 409


# ============================================================================================== conversion
async def sold_simple_product(client, admin_headers, category, provider, *, stock=10, sold=2, fulfil=True):
    """A simple product with an order already placed, paid and (optionally) shipped."""
    p = await make_product(client, admin_headers, category, stock=stock, status="active", name="Linen Shirt")
    headers, _ = await signup(client)
    await add(client, headers, product_id=p["id"], quantity=sold)
    order = (await checkout(client, headers)).json()
    await pay(client, headers, order["id"])
    await hook(client, provider.last_pi)
    if fulfil:
        assert (await client.patch(f"{API}/orders/{order['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)).status_code == 200
    return p, order


def convert_body(expected, specs=None, reason="launching sizes"):
    return {
        "options": [{"name": "Size", "values": ["M", "L"]}], "expected_stock": expected, "reason": reason,
        "variants": specs or [{"sku": "LS-M", "option_values": {"Size": "M"}, "stock": expected - 3 if expected >= 3 else 0},
                              {"sku": "LS-L", "option_values": {"Size": "L"}, "stock": min(3, expected)}],
    }


async def test_converting_a_product_moves_its_stock_exactly_and_keeps_its_history(client, admin_headers, category, provider, db_session):
    p, order = await sold_simple_product(client, admin_headers, category, provider)                  # stock 10 -> 8 after the sale; fulfilled
    other, _ = await signup(client)
    await add(client, other, product_id=p["id"], quantity=1)                                          # someone has it in their cart
    r = await client.post(f"{API}/products/{p['id']}/convert-to-variants", json=convert_body(8), headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cart_lines_removed"] == 1
    detail = body["product"]
    assert detail["has_variants"] is True and detail["stock"] == 8 and [(x["sku"], x["stock"]) for x in detail["variants"]] == [("LS-M", 5), ("LS-L", 3)]
    assert (await db_session.execute(text("SELECT stock, option_count FROM products WHERE id = :p"), {"p": p["id"]})).one() == (0, 1)
    moves = (await db_session.execute(text("SELECT kind, delta, variant_id IS NOT NULL, reason FROM stock_movements WHERE kind = 'conversion' ORDER BY id"))).all()
    assert [(m[0], m[1], m[2]) for m in moves] == [("conversion", -8, False), ("conversion", 5, True), ("conversion", 3, True)] and {m[3] for m in moves} == {"launching sizes"}
    history = (await client.get(f"{API}/orders/{order['id']}", headers=admin_headers)).json()["items"][0]
    assert history["variant_id"] is None and history["sku"] == p["sku"] and history["product_name_snapshot"] == "Linen Shirt"   # the old order is untouched
    assert (await client.get(f"{API}/cart", headers=other)).json()["items"] == []
    await assert_balanced(db_session)
    again = await client.post(f"{API}/products/{p['id']}/convert-to-variants", json=convert_body(8), headers=admin_headers)
    assert again.status_code == 409 and again.json()["code"] == "already_has_variants"


async def test_a_conversion_that_cannot_be_exact_changes_nothing(client, admin_headers, category, provider, db_session):
    p, _ = await sold_simple_product(client, admin_headers, category, provider)                        # stock is now 8
    url = f"{API}/products/{p['id']}/convert-to-variants"
    stale = await client.post(url, json=convert_body(10), headers=admin_headers)                       # you saw 10, it is 8
    assert stale.status_code == 409 and stale.json()["code"] == "stock_changed" and "8" in stale.json()["detail"]
    off = convert_body(8, [{"sku": "LS-M", "option_values": {"Size": "M"}, "stock": 9}, {"sku": "LS-L", "option_values": {"Size": "L"}, "stock": 0}])
    mismatch = await client.post(url, json=off, headers=admin_headers)
    assert mismatch.status_code == 422 and mismatch.json()["code"] == "stock_allocation_mismatch"      # 9 is not 8: nothing invented or lost
    other, _ = await signup(client)
    await add(client, other, product_id=p["id"], quantity=1)
    dup_sku = convert_body(8, [{"sku": "LS-M", "option_values": {"Size": "M"}, "stock": 5}, {"sku": "ls-m", "option_values": {"Size": "L"}, "stock": 3}])
    failed = await client.post(url, json=dup_sku, headers=admin_headers)
    assert failed.status_code == 422                                                                    # fails after the checks: everything rolls back
    assert (await db_session.execute(text("SELECT stock, option_count FROM products WHERE id = :p"), {"p": p["id"]})).one() == (8, 0)
    assert (await db_session.execute(text("SELECT count(*) FROM product_options WHERE product_id = :p"), {"p": p["id"]})).scalar_one() == 0
    assert len((await client.get(f"{API}/cart", headers=other)).json()["items"]) == 1                    # its cart line was NOT removed
    assert (await db_session.execute(text("SELECT count(*) FROM stock_movements WHERE kind = 'conversion'"))).scalar_one() == 0
    collide = convert_body(8)
    collide["variants"][0]["sku"] = p["sku"]                                                            # a SKU the product itself already owns
    assert (await client.post(url, json=collide, headers=admin_headers)).status_code == 409


async def test_a_product_with_an_order_that_could_still_be_restocked_cannot_be_converted(client, admin_headers, category, provider):
    unshipped, _ = await sold_simple_product(client, admin_headers, category, provider, fulfil=False)   # paid, not shipped
    r = await client.post(f"{API}/products/{unshipped['id']}/convert-to-variants", json=convert_body(8), headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "product_has_open_orders"
    pending = await make_product(client, admin_headers, category, stock=10, status="active")
    headers, _ = await signup(client)
    await add(client, headers, product_id=pending["id"], quantity=1)
    await checkout(client, headers)                                                                     # unpaid
    assert (await client.post(f"{API}/products/{pending['id']}/convert-to-variants", json=convert_body(9), headers=admin_headers)).status_code == 409
    archived = await make_product(client, admin_headers, category, stock=4, status="active")
    await client.delete(f"{API}/products/{archived['id']}", headers=admin_headers)
    assert (await client.post(f"{API}/products/{archived['id']}/convert-to-variants", json=convert_body(4), headers=admin_headers)).status_code == 422


async def test_two_simultaneous_conversions_of_one_product_have_one_winner(client, admin_headers, category, db_session):
    p = await make_product(client, admin_headers, category, stock=6, status="active")
    a = convert_body(6, [{"sku": "W-M", "option_values": {"Size": "M"}, "stock": 6}, {"sku": "W-L", "option_values": {"Size": "L"}, "stock": 0}])
    b = convert_body(6, [{"sku": "X-M", "option_values": {"Size": "M"}, "stock": 6}, {"sku": "X-L", "option_values": {"Size": "L"}, "stock": 0}])
    results = await asyncio.gather(*[client.post(f"{API}/products/{p['id']}/convert-to-variants", json=body, headers=admin_headers) for body in (a, b)])
    assert sorted(r.status_code for r in results) == [200, 409]
    assert (await db_session.execute(text("SELECT count(*) FROM product_variants WHERE product_id = :p"), {"p": p["id"]})).scalar_one() == 2
    await assert_balanced(db_session)


# ============================================================================================== reports, cache, audit
async def test_reports_list_variants_and_can_group_sales_by_variant(client, admin_headers, category, provider, db_session):
    detail = await tee(client, admin_headers, category, status="active", stock_each=6)
    pid, vs = detail["id"], variants_by_sku(detail)
    low = (await client.get(f"{API}/admin/reports/low-stock", params={"threshold": 100}, headers=admin_headers)).json()
    ours = [i for i in low["items"] if i["id"] == pid]
    assert sorted(i["sku"] for i in ours) == ["T-L-BLK", "T-M-BLK", "T-M-BLU", "T-S-BLK"]            # the variants, never the parent
    entry = next(i for i in ours if i["sku"] == "T-M-BLU")
    assert (entry["variant_id"], entry["options"], entry["stock"], entry["price_cents"]) == (vs["T-M-BLU"]["id"], "Size: M / Colour: Blue", 6, 1000)
    assert next(i for i in ours if i["sku"] == "T-M-BLK")["price_cents"] == 900                     # a variant's own price

    buyer, _ = await signup(client)
    await add(client, buyer, variant_id=vs["T-M-BLK"]["id"], quantity=2)
    await add(client, buyer, variant_id=vs["T-M-BLU"]["id"], quantity=1)
    order = (await checkout(client, buyer)).json()
    await pay(client, buyer, order["id"])
    assert (await hook(client, provider.last_pi)).status_code == 200
    after = (await client.get(f"{API}/admin/reports/low-stock", params={"threshold": 5}, headers=admin_headers)).json()
    assert {(i["sku"], i["stock"]) for i in after["items"] if i["id"] == pid} == {("T-M-BLK", 4), ("T-M-BLU", 5)}

    merged = (await client.get(f"{API}/admin/reports/top-products", headers=admin_headers)).json()      # the default: one row per product
    assert [(i["units"], i["revenue_cents"], i["variant_id"]) for i in merged["items"] if i["product_id"] == pid] == [(3, 2 * 900 + 1000, None)]
    split = (await client.get(f"{API}/admin/reports/top-products", params={"group_by": "variant"}, headers=admin_headers)).json()
    rows = {i["sku"]: i for i in split["items"] if i["product_id"] == pid}
    assert split["group_by"] == "variant" and set(rows) == {"T-M-BLK", "T-M-BLU"}
    assert (rows["T-M-BLK"]["units"], rows["T-M-BLK"]["revenue_cents"], rows["T-M-BLK"]["variant_label"], rows["T-M-BLK"]["current_stock"]) == (2, 1800, "Size: M / Colour: Black", 4)
    assert rows["T-M-BLU"]["variant_id"] == vs["T-M-BLU"]["id"] and rows["T-M-BLU"]["variant_label"] == "Size: M / Colour: Blue"


async def test_admin_changes_invalidate_the_cached_public_lists(client, admin_headers, category, cache):
    detail = await tee(client, admin_headers, category, status="active")
    pid = detail["id"]
    first = await client.get(f"{API}/products")
    assert first.headers["X-Cache"] == "MISS" and (await client.get(f"{API}/products")).headers["X-Cache"] == "HIT"
    assert next(i for i in first.json()["items"] if i["id"] == pid)["price_from_cents"] == 900
    await client.patch(f"{API}/products/{pid}/variants/{variants_by_sku(detail)['T-M-BLK']['id']}", json={"price_override_cents": 400}, headers=admin_headers)
    after = await client.get(f"{API}/products")
    assert after.headers["X-Cache"] == "MISS" and next(i for i in after.json()["items"] if i["id"] == pid)["price_from_cents"] == 400


async def test_every_administrative_change_is_audited_without_secrets(client, admin_headers, category, db_session):
    detail = await tee(client, admin_headers, category, status="active")
    await client.post(f"{API}/products/{detail['id']}/variants/{detail['variants'][0]['id']}/stock", json={"delta": 1, "reason": "audit me"}, headers=admin_headers)
    await db_session.rollback()
    actions = {r[0] for r in (await db_session.execute(text("SELECT action FROM audit_logs"))).all()}
    assert {"product.options_set", "variant.created", "stock.adjusted"} <= actions
