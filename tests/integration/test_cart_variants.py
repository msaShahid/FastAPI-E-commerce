# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""E3: the cart with variants: choosing one, effective prices, availability, merging, the digest, and the temporary checkout guard."""

import uuid
from types import SimpleNamespace

from sqlalchemy import text
from test_order_payment_lifecycle import category  # noqa: F401
from test_query_counts import count_statements, statements  # noqa: F401
from test_variant_pricing import option_product, simple_product

from app.core.rate_limit import limiter
from app.modules.cart.services.cart_digest import cart_digest

API = "/api/v1"


async def signup(client):
    limiter.reset()
    name = f"u{uuid.uuid4().hex[:8]}"
    email = f"{name}@example.com"
    await client.post(f"{API}/auth/register", json={"username": name, "email": email, "password": "CorrectHorse1!"})
    client.cookies.clear()
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": "CorrectHorse1!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, email


async def add(client, headers=None, **body):
    return await client.post(f"{API}/cart/items", json=body, headers=headers or {})


async def cart(client, headers=None):
    r = await client.get(f"{API}/cart", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def set_variant(db, variant_id, **fields):
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    await db.execute(text(f"UPDATE product_variants SET {sets} WHERE id = :id"), {"id": variant_id, **fields})
    await db.commit()


# ============================================================================================== choosing a variant
async def test_a_variant_is_chosen_by_id_and_costs_its_effective_price(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(700, 5, "active"), (None, 5, "active")], base=1000)
    headers, _ = await signup(client)
    assert (await add(client, headers, variant_id=a, quantity=2)).status_code == 201
    body = (await add(client, headers, variant_id=b, quantity=1)).json()
    lines = {line["variant"]["id"]: line for line in body["items"]}
    assert lines[a]["price_cents_snapshot"] == 700 and lines[a]["current_price_cents"] == 700 and lines[a]["line_total_cents"] == 1400
    assert lines[b]["price_cents_snapshot"] == 1000 and lines[b]["quantity"] == 1                 # no override: the product's price
    assert body["subtotal_cents"] == 2400 and body["item_count"] == 2
    line = lines[a]
    assert line["variant"]["sku"].startswith("PV-") and line["variant"]["options"] == [{"name": "Size", "value": "S1"}]
    assert (line["price_changed"], line["available"], line["unavailable_reason"], line["product"]["price_cents"]) == (False, True, None, 1000)


async def test_a_client_cannot_choose_the_price_and_a_mismatched_product_is_refused(client, db_session, category):
    pid, (a,) = await option_product(db_session, category, [(700, 5, "active")])
    other = await simple_product(db_session, category)
    headers, _ = await signup(client)
    assert (await add(client, headers, product_id=pid, variant_id=a, quantity=1)).status_code == 201       # both ids, consistent
    body = {"variant_id": a, "quantity": 1, "price_cents": 1, "price_cents_snapshot": 1}
    assert (await add(client, headers, **body)).json()["items"][0]["price_cents_snapshot"] == 700           # extra fields are ignored
    wrong = await add(client, headers, product_id=other, variant_id=a, quantity=1)
    assert wrong.status_code == 422 and wrong.json()["code"] == "variant_unavailable"
    assert (await add(client, headers, quantity=1)).status_code == 422                                      # neither id


async def test_a_product_with_options_cannot_be_added_without_choosing_one_and_simple_products_work_as_before(client, db_session, category):
    pid, _ = await option_product(db_session, category, [(None, 5, "active")])
    simple = await simple_product(db_session, category, price=1500, stock=3)
    headers, _ = await signup(client)
    r = await add(client, headers, product_id=pid, quantity=1)
    assert r.status_code == 422 and r.json()["code"] == "variant_required"
    ok = await add(client, headers, product_id=simple, quantity=2)
    assert ok.status_code == 201 and ok.json()["items"][0]["variant"] is None and ok.json()["items"][0]["price_cents_snapshot"] == 1500


async def test_every_way_a_variant_cannot_be_bought_gives_the_same_answer(client, db_session, category):
    pid, (inactive, active) = await option_product(db_session, category, [(None, 5, "inactive"), (None, 5, "active")])
    draft, (in_draft,) = await option_product(db_session, category, [(None, 5, "active")], status="draft")
    headers, _ = await signup(client)
    for variant in (inactive, in_draft, 999999):
        r = await add(client, headers, variant_id=variant, quantity=1)
        assert r.status_code == 422 and r.json()["code"] == "variant_unavailable"
    assert (await add(client, headers, variant_id=active, quantity=1)).status_code == 201


# ============================================================================================== stock
async def test_stock_is_checked_against_the_variant_including_what_is_already_in_the_cart(client, db_session, category):
    pid, (low, high) = await option_product(db_session, category, [(None, 4, "active"), (None, 50, "active")])
    headers, _ = await signup(client)
    over = await add(client, headers, variant_id=low, quantity=5)
    assert over.status_code == 422 and "Only 4 unit(s)" in over.json()["detail"]          # low stock: the count is named
    far_over = await add(client, headers, variant_id=high, quantity=60)
    assert far_over.status_code == 422 and far_over.json()["detail"] == "Not enough stock for that quantity"   # plenty in stock: no number
    assert (await add(client, headers, variant_id=low, quantity=3)).status_code == 201
    again = await add(client, headers, variant_id=low, quantity=2)                         # 3 + 2 > 4
    assert again.status_code == 422
    sum_up = await add(client, headers, variant_id=low, quantity=1)
    lines = sum_up.json()["items"]
    assert len(lines) == 1 and lines[0]["quantity"] == 4                                    # the same variant stays ONE line
    other = await add(client, headers, variant_id=high, quantity=1)
    assert len(other.json()["items"]) == 2                                                  # another variant of the product is another line


async def test_changing_a_quantity_is_checked_against_the_variants_stock_and_belongs_to_its_owner(client, db_session, category):
    pid, (v,) = await option_product(db_session, category, [(None, 4, "active")])
    headers, _ = await signup(client)
    line = (await add(client, headers, variant_id=v, quantity=1)).json()["items"][0]["id"]
    assert (await client.patch(f"{API}/cart/items/{line}", json={"quantity": 4}, headers=headers)).status_code == 200
    assert (await client.patch(f"{API}/cart/items/{line}", json={"quantity": 5}, headers=headers)).status_code == 422
    stranger, _ = await signup(client)
    assert (await client.patch(f"{API}/cart/items/{line}", json={"quantity": 1}, headers=stranger)).status_code == 404
    assert (await client.delete(f"{API}/cart/items/{line}", headers=headers)).json()["items"] == []


# ============================================================================================== price and availability changes
async def test_price_changed_follows_the_effective_price_not_the_products_price(client, db_session, category):
    pid, (override, follows) = await option_product(db_session, category, [(700, 5, "active"), (None, 5, "active")], base=1000)
    headers, _ = await signup(client)
    await add(client, headers, variant_id=override, quantity=1)
    await add(client, headers, variant_id=follows, quantity=1)
    await db_session.execute(text("UPDATE products SET price_cents = 1400 WHERE id = :p"), {"p": pid})
    await db_session.commit()
    lines = {line["variant"]["id"]: line for line in (await cart(client, headers))["items"]}
    assert (lines[override]["price_changed"], lines[override]["current_price_cents"]) == (False, 700)    # its override did not move
    assert (lines[follows]["price_changed"], lines[follows]["current_price_cents"], lines[follows]["price_cents_snapshot"]) == (True, 1400, 1000)
    await set_variant(db_session, override, price_override_cents=650)
    lines = {line["variant"]["id"]: line for line in (await cart(client, headers))["items"]}
    assert (lines[override]["price_changed"], lines[override]["current_price_cents"]) == (True, 650)


async def test_a_line_that_can_no_longer_be_bought_says_so_and_the_cart_still_loads(client, db_session, category):
    pid, (v1, v2, v3, v4) = await option_product(db_session, category, [(None, 5, "active")] * 4)
    headers, _ = await signup(client)
    for v in (v1, v2, v3, v4):
        await add(client, headers, variant_id=v, quantity=3)
    await set_variant(db_session, v1, stock=0)
    await set_variant(db_session, v2, status="inactive")
    await set_variant(db_session, v3, stock=2)
    lines = {line["variant"]["id"]: line for line in (await cart(client, headers))["items"]}
    assert [(lines[v]["available"], lines[v]["unavailable_reason"]) for v in (v1, v2, v3, v4)] == [
        (False, "out_of_stock"), (False, "unavailable"), (False, "insufficient_stock"), (True, None)]
    await db_session.execute(text("UPDATE products SET status = 'archived' WHERE id = :p"), {"p": pid})
    await db_session.commit()
    assert all(line["unavailable_reason"] == "unavailable" for line in (await cart(client, headers))["items"])


# ============================================================================================== merging, digest, checkout guard
async def test_a_guest_cart_merges_by_variant_into_the_users_cart(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 20, "active"), (None, 20, "active")])
    simple = await simple_product(db_session, category, stock=20)
    headers, email = await signup(client)
    await add(client, headers, variant_id=a, quantity=2)                      # the user's own cart
    client.cookies.clear()
    await add(client, None, variant_id=a, quantity=1)                         # then, anonymously (a guest cart cookie is set)
    await add(client, None, variant_id=b, quantity=4)
    await add(client, None, product_id=simple, quantity=1)
    login = await client.post(f"{API}/auth/login", json={"email": email, "password": "CorrectHorse1!"})   # logging in carries the guest cart over
    assert login.status_code == 200
    merged = {(line["variant"] or {}).get("id"): line["quantity"] for line in (await cart(client, headers))["items"]}
    assert merged == {a: 3, b: 4, None: 1}                                    # the same variant summed, another kept apart, a simple product intact


async def test_the_cart_digest_describes_products_variants_and_quantities_only(client, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 20, "active"), (None, 20, "active")])
    headers, _ = await signup(client)
    empty = (await cart(client, headers))["cart_digest"]
    await add(client, headers, variant_id=a, quantity=1)
    one = (await cart(client, headers))["cart_digest"]
    await add(client, headers, variant_id=b, quantity=2)
    two = (await cart(client, headers))["cart_digest"]
    assert len({empty, one, two}) == 3
    other, _ = await signup(client)
    await add(client, other, variant_id=b, quantity=2)
    await add(client, other, variant_id=a, quantity=1)                         # the same lines added in the opposite order
    assert (await cart(client, other))["cart_digest"] == two
    await db_session.execute(text("UPDATE products SET price_cents = 1234 WHERE id = :p"), {"p": pid})   # a price change is not a different cart
    await db_session.commit()
    assert (await cart(client, other))["cart_digest"] == two
    third, _ = await signup(client)
    await add(client, third, variant_id=b, quantity=1)
    await add(client, third, variant_id=a, quantity=2)                         # same variants, swapped quantities: a different cart
    assert (await cart(client, third))["cart_digest"] != two


def test_the_digest_is_independent_of_line_order_and_distinguishes_variants_and_simple_lines():
    def line(product, variant, quantity):
        return SimpleNamespace(product_id=product, variant_id=variant, quantity=quantity)

    assert cart_digest([line(1, None, 2), line(2, 5, 1)]) == cart_digest([line(2, 5, 1), line(1, None, 2)])
    assert cart_digest([line(1, None, 2)]) != cart_digest([line(1, 7, 2)]) != cart_digest([line(1, None, 3)])
    assert cart_digest([]) == cart_digest([])


async def test_reading_a_cart_costs_a_constant_number_of_queries_however_many_variants_it_holds(client, db_session, category, statements):
    pid, ids = await option_product(db_session, category, [(None, 50, "active")] * 8)
    small_headers, _ = await signup(client)
    large_headers, _ = await signup(client)
    await add(client, small_headers, variant_id=ids[0], quantity=1)
    for v in ids:
        await add(client, large_headers, variant_id=v, quantity=1)
    small = await count_statements(statements, lambda: client.get(f"{API}/cart", headers=small_headers))
    large = await count_statements(statements, lambda: client.get(f"{API}/cart", headers=large_headers))
    assert small == large <= 7                       # user, cart, items, products, variants, option values
