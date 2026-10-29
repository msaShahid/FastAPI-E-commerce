# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""E2: effective prices, price ranges, price filtering and sorting, public availability instead of exact stock, and cache correctness."""

import asyncio

from sqlalchemy import text, update
from test_cache_integration import cache, redis_client  # noqa: F401
from test_order_payment_lifecycle import category  # noqa: F401
from test_query_counts import count_statements, statements  # noqa: F401
from test_variant_schema import new_product, refused, with_options

from app.core.cache import flush_pending
from app.core.config import get_settings
from app.modules.products.models.variants import ProductVariant

API = "/api/v1"


async def option_product(db, category_id, variants, *, base=1000, status="active", name=None):
    """A product with ONE option (Size) and the given variants: (override_price | None, stock, status). Returns (product_id, [variant_ids])."""
    from test_variant_schema import variant

    sizes = [f"S{i}" for i in range(1, len(variants) + 1)]
    pid, values = await with_options(db, category_id, {"Size": sizes})
    ids = []
    for i, (override, stock, vstatus) in enumerate(variants, start=1):
        ids.append(await variant(db, pid, f"PV-{pid}-{i}", [values["Size", f"S{i}"]], stock=stock, status=vstatus, price=override))
    await db.execute(text("UPDATE products SET price_cents = :b, status = CAST(:s AS product_status)" + (", name = :n" if name else "") + " WHERE id = :p"),
                     {"b": base, "s": status, "p": pid, **({"n": name} if name else {})})
    await db.commit()
    return pid, ids


async def simple_product(db, category_id, price=1000, stock=10, status="active", name=None):
    pid = await new_product(db, category_id, stock=stock, status=status)
    await db.execute(text("UPDATE products SET price_cents = :b" + (", name = :n" if name else "") + " WHERE id = :p"), {"b": price, "p": pid, **({"n": name} if name else {})})
    await db.commit()
    return pid


async def db_range(db, pid):
    await db.rollback()
    return tuple((await db.execute(text("SELECT price_from_cents, price_to_cents FROM products WHERE id = :p"), {"p": pid})).one())


async def listing(client, headers=None, **params):
    r = await client.get(f"{API}/products", params={"page_size": 100, **params}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ============================================================================================== the database keeps the range
async def test_the_range_of_a_simple_product_is_its_own_price_and_follows_edits(db_session, category):
    pid = await simple_product(db_session, category, price=1000)
    assert await db_range(db_session, pid) == (1000, 1000)
    await db_session.execute(text("UPDATE products SET price_cents = 1500 WHERE id = :p"), {"p": pid})
    await db_session.commit()
    assert await db_range(db_session, pid) == (1500, 1500)


async def test_the_range_of_a_product_with_options_covers_its_active_variants_in_stock_or_not(db_session, category):
    pid, (a, b, c) = await option_product(db_session, category, [(800, 0, "active"), (None, 5, "active"), (2000, 9, "inactive")], status="draft")
    assert await db_range(db_session, pid) == (800, 1000)         # the out-of-stock 800 counts; the inactive 2000 does not
    await db_session.execute(text("UPDATE products SET price_cents = 1200 WHERE id = :p"), {"p": pid})   # variants WITHOUT an override follow the base price
    await db_session.commit()
    assert await db_range(db_session, pid) == (800, 1200)
    await db_session.execute(text("UPDATE product_variants SET price_override_cents = 300 WHERE id = :v"), {"v": b})
    await db_session.commit()
    assert await db_range(db_session, pid) == (300, 800)
    await db_session.execute(text("UPDATE product_variants SET status = 'inactive' WHERE id IN (:a, :b)"), {"a": a, "b": b})
    await db_session.commit()
    assert await db_range(db_session, pid) == (1200, 1200)        # nothing active to price: the base price stands
    await db_session.execute(text("UPDATE product_variants SET status = 'active', price_override_cents = 50 WHERE id = :c"), {"c": c})
    await db_session.commit()
    assert await db_range(db_session, pid) == (50, 50)
    await db_session.execute(text("DELETE FROM product_variants WHERE id = :a"), {"a": a})   # `a` never held stock: no history, so it can go
    await db_session.commit()
    assert await db_range(db_session, pid) == (50, 50)                                         # (it was inactive: no change)
    await refused(db_session, db_session.execute(text("DELETE FROM product_variants WHERE id = :c"), {"c": c}))   # `c` has stock history: the ledger keeps it
    await db_session.execute(text("UPDATE product_variants SET status = 'inactive' WHERE id = :c"), {"c": c})
    await db_session.commit()
    assert await db_range(db_session, pid) == (1200, 1200)


async def test_a_price_edit_and_an_override_edit_at_the_same_time_end_consistent(test_session_factory, db_session, category):
    pid, (a, b) = await option_product(db_session, category, [(None, 5, "active"), (900, 5, "active")], status="draft")
    s1, s2 = test_session_factory(), test_session_factory()
    try:
        await s1.execute(text("UPDATE products SET price_cents = 2000 WHERE id = :p"), {"p": pid})
        task = asyncio.create_task(s2.execute(text("UPDATE product_variants SET price_override_cents = 100 WHERE id = :v"), {"v": b}))
        await asyncio.sleep(0.4)
        assert not task.done()            # serialised on the product row, not racing
        await s1.commit()
        await task
        await s2.commit()
    finally:
        await s1.close()
        await s2.close()
    await db_session.rollback()
    expected = (await db_session.execute(text("SELECT price_from, price_to FROM product_price_range(:p, 2000, 1::smallint)"), {"p": pid})).one()
    assert await db_range(db_session, pid) == tuple(expected) == (100, 2000)


# ============================================================================================== filtering and sorting
async def test_price_filters_match_an_actual_variant_price_not_a_range_overlap(client, db_session, category):
    plain = await simple_product(db_session, category, price=700, name="Plain 700")
    gap, _ = await option_product(db_session, category, [(400, 5, "active"), (1200, 5, "active")], name="Gap 400/1200")
    hidden, _ = await option_product(db_session, category, [(900, 5, "inactive"), (2000, 5, "active")], name="Hidden 900/2000")

    async def names(**params):
        return sorted(i["name"] for i in await listing(client, **params))

    assert await names(min_price=500, max_price=1000) == ["Plain 700"]               # 400 and 1200 straddle the range: no variant is inside it
    assert await names(min_price=300, max_price=500) == ["Gap 400/1200"]
    assert await names(min_price=1100, max_price=1300) == ["Gap 400/1200"]
    assert await names(min_price=1100) == ["Gap 400/1200", "Hidden 900/2000"]        # open-ended
    assert await names(max_price=600) == ["Gap 400/1200"]
    assert await names(min_price=800, max_price=1000) == []                          # Hidden's 900 is inactive: not purchasable, not matched
    assert await names(min_price=700, max_price=700) == ["Plain 700"]                # simple products: exactly as before
    assert {plain, gap, hidden}  # (ids used only to build the catalog)


async def test_price_sorting_uses_the_effective_range_and_is_stable(client, db_session, category):
    await simple_product(db_session, category, price=700, name="Simple 700")
    await option_product(db_session, category, [(400, 5, "active"), (1200, 5, "active")], name="Range 400-1200")
    await option_product(db_session, category, [(900, 5, "active")], name="Single 900")
    await simple_product(db_session, category, price=700, name="Simple 700 too")
    ascending = [i["name"] for i in await listing(client, sort="price_cents")]
    assert ascending == ["Range 400-1200", "Simple 700", "Simple 700 too", "Single 900"]      # lowest price first; ties in creation order
    descending = [i["name"] for i in await listing(client, sort="-price_cents")]
    assert descending == ["Range 400-1200", "Single 900", "Simple 700", "Simple 700 too"]      # highest price first (1200, 900, 700, 700)


async def test_the_api_range_equals_the_database_range(client, db_session, category):
    ids = [
        await simple_product(db_session, category, price=700),
        (await option_product(db_session, category, [(400, 0, "active"), (None, 5, "active"), (3000, 5, "inactive")], base=1000))[0],
        (await option_product(db_session, category, [(250, 5, "active")], base=999))[0],
    ]
    by_id = {i["id"]: i for i in await listing(client)}
    for pid in ids:
        item = by_id[pid]
        assert (item["price_from_cents"], item["price_to_cents"]) == await db_range(db_session, pid)


# ============================================================================================== what the public and admins see
async def test_the_public_sees_availability_not_stock_and_admins_see_both(client, db_session, category, admin_headers):
    pid = await simple_product(db_session, category, price=1000, stock=3)
    out = await simple_product(db_session, category, price=1000, stock=0)
    big = await simple_product(db_session, category, price=1000, stock=50)
    public = {i["id"]: i for i in await listing(client)}
    assert [(public[p]["stock"], public[p]["available"], public[p]["availability"]) for p in (pid, out, big)] == [
        (None, True, "low_stock"), (None, False, "out_of_stock"), (None, True, "in_stock")]
    assert public[pid]["base_price_cents"] is None and public[pid]["has_variants"] is False
    detail = (await client.get(f"{API}/products/{pid}")).json()
    assert detail["stock"] is None and detail["availability"] == "low_stock"
    admin = {i["id"]: i for i in await listing(client, headers=admin_headers)}
    assert [admin[p]["stock"] for p in (pid, out, big)] == [3, 0, 50] and admin[pid]["base_price_cents"] == 1000


async def test_public_stock_visibility_and_the_low_stock_threshold_are_configurable(client, db_session, category, monkeypatch):
    pid, _ = await option_product(db_session, category, [(None, 4, "active"), (None, 6, "inactive")])
    assert (await client.get(f"{API}/products/{pid}")).json()["stock"] is None                       # the default hides it
    monkeypatch.setattr(get_settings(), "public_stock_visibility", "exact")
    shown = (await client.get(f"{API}/products/{pid}")).json()
    assert shown["stock"] == 4 and [v["stock"] for v in shown["variants"]] == [4]                    # active variants only, for the public
    monkeypatch.setattr(get_settings(), "low_stock_display_threshold", 3)
    assert (await client.get(f"{API}/products/{pid}")).json()["availability"] == "in_stock"          # 4 is above a threshold of 3


async def test_a_product_with_options_shows_a_from_price_a_range_and_its_best_variants_availability(client, db_session, category, admin_headers):
    pid, (a, b, c) = await option_product(db_session, category, [(800, 0, "active"), (None, 3, "active"), (2000, 9, "inactive")], base=1000)
    item = next(i for i in await listing(client) if i["id"] == pid)
    assert (item["price_cents"], item["price_from_cents"], item["price_to_cents"], item["has_price_range"]) == (800, 800, 1000, True)
    assert (item["has_variants"], item["variant_count"], item["availability"], item["available"], item["stock"]) == (True, 2, "low_stock", True, None)
    assert item["variants"] is None and item["options"] is None               # the list stays light: detail has the variants

    detail = (await client.get(f"{API}/products/{pid}")).json()
    assert detail["options"] == [{"name": "Size", "values": ["S1", "S2", "S3"]}]
    assert [(v["id"], v["price_cents"], v["price_overridden"], v["available"], v["availability"], v["stock"], v["status"]) for v in detail["variants"]] == [
        (a, 800, True, False, "out_of_stock", None, None), (b, 1000, False, True, "low_stock", None, None)]    # the inactive one is not shown
    assert detail["variants"][0]["options"] == [{"name": "Size", "value": "S1"}] and detail["variants"][0]["sku"].startswith("PV-")

    admin = (await client.get(f"{API}/products/{pid}", headers=admin_headers)).json()
    assert admin["stock"] == 12 and admin["base_price_cents"] == 1000
    assert [(v["id"], v["price_cents"], v["stock"], v["status"]) for v in admin["variants"]] == [(a, 800, 0, "active"), (b, 1000, 3, "active"), (c, 2000, 9, "inactive")]


async def test_a_product_whose_variants_are_all_out_of_stock_is_listed_as_unavailable_not_hidden(client, db_session, category):
    pid, _ = await option_product(db_session, category, [(500, 0, "active"), (700, 0, "active")])
    item = next(i for i in await listing(client) if i["id"] == pid)
    assert (item["available"], item["availability"], item["price_from_cents"], item["price_to_cents"]) == (False, "out_of_stock", 500, 700)


async def test_drafts_stay_hidden_from_the_public_and_an_admin_edit_returns_the_variants(client, db_session, category, admin_headers):
    pid, _ = await option_product(db_session, category, [(None, 5, "active")], status="draft")
    assert (await client.get(f"{API}/products/{pid}")).status_code == 404 and pid not in [i["id"] for i in await listing(client)]
    r = await client.patch(f"{API}/products/{pid}", headers=admin_headers, json={"price_cents": 1300})
    assert r.status_code == 200 and r.json()["variants"][0]["price_cents"] == 1300 and r.json()["price_from_cents"] == 1300   # variants without an override follow


# ============================================================================================== cache and cost
async def test_cached_public_lists_follow_variant_changes_and_never_hold_exact_stock(client, db_session, category, admin_headers, cache):
    pid, (v1, _v2) = await option_product(db_session, category, [(500, 7, "active"), (900, 7, "active")])
    first = await client.get(f"{API}/products")
    assert first.headers["X-Cache"] == "MISS" and (await client.get(f"{API}/products")).headers["X-Cache"] == "HIT"
    body = next(i for i in first.json()["items"] if i["id"] == pid)
    assert body["price_from_cents"] == 500 and body["stock"] is None and '"stock":7' not in first.text.replace(" ", "")
    admin = next(i for i in await listing(client, headers=admin_headers) if i["id"] == pid)
    assert admin["stock"] == 14                                           # admins are never served the cached public view

    await db_session.execute(update(ProductVariant).where(ProductVariant.id == v1).values(price_override_cents=300))   # a bulk ORM write
    await db_session.commit()
    await flush_pending(db_session)
    after = await client.get(f"{API}/products")
    assert after.headers["X-Cache"] == "MISS"
    assert next(i for i in after.json()["items"] if i["id"] == pid)["price_from_cents"] == 300


async def test_listing_cost_is_constant_for_simple_catalogs_and_for_variant_catalogs(client, db_session, category, statements):
    for i in range(6):
        await simple_product(db_session, category, price=500 + i)
    small = await count_statements(statements, lambda: client.get(f"{API}/products?page_size=2"))
    large = await count_statements(statements, lambda: client.get(f"{API}/products?page_size=100"))
    assert small == large <= 3                                            # count + page + categories: simple products cost nothing extra
    for _ in range(6):
        await option_product(db_session, category, [(None, 5, "active"), (900, 5, "active")])
    small = await count_statements(statements, lambda: client.get(f"{API}/products?page_size=2"))
    large = await count_statements(statements, lambda: client.get(f"{API}/products?page_size=100"))
    assert small == large <= 4                                            # + ONE query for every variant on the page
