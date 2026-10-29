# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""E1: the database itself refuses inconsistent product/variant states. Application code is deliberately bypassed (raw SQL): these are
the rules that hold even if a service has a bug, and under concurrent writers."""

import asyncio
import itertools

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_order_payment_lifecycle import category, make_order  # noqa: F401

from app.core.rate_limit import limiter

_n = itertools.count(1)


async def new_product(db, category_id, sku=None, *, stock=0, status="draft"):
    i = next(_n)
    return (await db.execute(text(
        "INSERT INTO products (name, slug, price_cents, sku, stock, status, category_id) "
        "VALUES (:n, :s, 1000, :sku, :stock, CAST(:st AS product_status), :c) RETURNING id"),
        {"n": f"P{i}", "s": f"p-{i}-{next(_n)}", "sku": sku or f"SKU-{i}", "stock": stock, "st": status, "c": category_id})).scalar_one()


async def with_options(db, category_id, options: dict[str, list[str]], sku=None):
    """A product with its option sets, NOT yet any variants. Returns (product_id, {(option, value): value_id})."""
    pid = await new_product(db, category_id, sku)
    values = {}
    for position, (name, vals) in enumerate(options.items(), start=1):
        oid = (await db.execute(text("INSERT INTO product_options (product_id, position, name) VALUES (:p, :pos, :n) RETURNING id"),
                                {"p": pid, "pos": position, "n": name})).scalar_one()
        for v in vals:
            values[(name, v)] = (await db.execute(text(
                "INSERT INTO product_option_values (option_id, product_id, option_position, value) VALUES (:o, :p, :pos, :v) RETURNING id"),
                {"o": oid, "p": pid, "pos": position, "v": v})).scalar_one()
    await db.execute(text("UPDATE products SET option_count = :n WHERE id = :p"), {"n": len(options), "p": pid})
    return pid, values


async def variant(db, pid, sku, value_ids, *, option_count=None, stock=0, status="active", price=None):
    ids = list(value_ids) + [None] * (3 - len(value_ids))
    return (await db.execute(text(
        "INSERT INTO product_variants (product_id, option_count, value1_id, value2_id, value3_id, sku, stock, status, price_override_cents) "
        "VALUES (:p, :oc, :v1, :v2, :v3, :sku, :stock, :st, :price) RETURNING id"),
        {"p": pid, "oc": option_count or len(value_ids), "v1": ids[0], "v2": ids[1], "v3": ids[2], "sku": sku, "stock": stock, "st": status,
         "price": price})).scalar_one()


async def refused(db, coro, error=IntegrityError):
    with pytest.raises(error):
        await coro
    await db.rollback()


SIZE_COLOUR = {"Size": ["S", "M", "L"], "Colour": ["Black", "Blue"]}


# ============================================================================================== combinations and ownership
async def test_a_combination_can_exist_only_once_even_with_unused_slots(db_session, category):
    pid, v = await with_options(db_session, category, SIZE_COLOUR)
    await variant(db_session, pid, "TS-S-BLK", [v["Size", "S"], v["Colour", "Black"]])
    await variant(db_session, pid, "TS-S-BLU", [v["Size", "S"], v["Colour", "Blue"]])  # a different combination is fine
    await db_session.commit()
    await refused(db_session, variant(db_session, pid, "TS-OTHER", [v["Size", "S"], v["Colour", "Black"]]))
    # one option only: slots 2 and 3 are NULL, and NULLs still count as equal
    p1, v1 = await with_options(db_session, category, {"Size": ["S", "M"]})
    await variant(db_session, p1, "ONE-S", [v1["Size", "S"]])
    await db_session.commit()
    await refused(db_session, variant(db_session, p1, "ONE-S-AGAIN", [v1["Size", "S"]]))


async def test_a_value_of_another_product_or_the_wrong_slot_is_refused(db_session, category):
    p1, v1 = await with_options(db_session, category, SIZE_COLOUR)
    p2, v2 = await with_options(db_session, category, SIZE_COLOUR)
    await db_session.commit()
    await refused(db_session, variant(db_session, p1, "X-1", [v2["Size", "S"], v1["Colour", "Black"]]))   # another product's value
    await refused(db_session, variant(db_session, p1, "X-2", [v1["Colour", "Black"], v1["Size", "S"]]))   # the Colour value in the Size slot
    ok = await variant(db_session, p1, "X-OK", [v1["Size", "S"], v1["Colour", "Black"]])
    assert ok


async def test_a_combination_must_be_complete_and_have_nothing_in_unused_slots(db_session, category):
    pid, v = await with_options(db_session, category, SIZE_COLOUR)
    await db_session.commit()
    await refused(db_session, variant(db_session, pid, "INC-1", [v["Size", "S"]], option_count=2))                         # Colour missing
    await refused(db_session, variant(db_session, pid, "INC-2", [v["Size", "S"], v["Colour", "Black"]], option_count=1))    # one value too many
    await refused(db_session, variant(db_session, pid, "INC-3", [v["Size", "S"], v["Colour", "Black"]], option_count=3))    # wrong declared count


async def test_the_option_structure_must_match_the_declared_count_at_commit(db_session, category):
    pid = await new_product(db_session, category)
    await db_session.execute(text("INSERT INTO product_options (product_id, position, name) VALUES (:p, 1, 'Size')"), {"p": pid})
    await refused(db_session, db_session.commit())  # an option row exists but option_count is still 0

    pid, _ = await with_options(db_session, category, {"Size": ["S"]})
    await db_session.execute(text("UPDATE products SET option_count = 2 WHERE id = :p"), {"p": pid})  # declares two, has one
    await refused(db_session, db_session.commit())

    pid = await new_product(db_session, category)
    await db_session.execute(text("INSERT INTO product_options (product_id, position, name) VALUES (:p, 2, 'Colour')"), {"p": pid})
    await db_session.execute(text("UPDATE products SET option_count = 1 WHERE id = :p"), {"p": pid})  # positions must run 1..n
    await refused(db_session, db_session.commit())


async def test_options_cannot_be_restructured_while_variants_exist_and_stock_stays_on_the_variants(db_session, category):
    pid, v = await with_options(db_session, category, SIZE_COLOUR)
    await variant(db_session, pid, "KEEP-1", [v["Size", "S"], v["Colour", "Black"]])
    await db_session.commit()
    await refused(db_session, db_session.execute(text("UPDATE products SET option_count = 1 WHERE id = :p"), {"p": pid}))
    await refused(db_session, db_session.execute(text("UPDATE products SET stock = 5 WHERE id = :p"), {"p": pid}))  # option products hold no stock
    await refused(db_session, db_session.execute(text("DELETE FROM product_option_values WHERE id = :v"), {"v": v["Size", "S"]}))


async def test_variant_values_are_checked(db_session, category):
    pid, v = await with_options(db_session, category, {"Size": ["S", "M", "L", "XL"]})
    await db_session.commit()
    S, M, L, XL = (v["Size", x] for x in ("S", "M", "L", "XL"))
    await refused(db_session, variant(db_session, pid, "V-1", [S], price=0))
    await refused(db_session, variant(db_session, pid, "V-2", [M], stock=-1))
    await refused(db_session, variant(db_session, pid, " V-3 ", [L]))
    await refused(db_session, variant(db_session, pid, "V-4", [XL], status="retired"))
    await refused(db_session, db_session.execute(text("INSERT INTO product_options (product_id, position, name) VALUES (:p, 4, 'Four')"), {"p": pid}))
    await refused(db_session, db_session.execute(text("INSERT INTO product_options (product_id, position, name) VALUES (:p, 1, 'size')"), {"p": pid}))  # name clash, any case


# ============================================================================================== SKUs
async def test_skus_are_unique_across_products_and_variants_in_any_letter_case_and_never_change(db_session, category):
    pid, v = await with_options(db_session, category, {"Size": ["S", "M"]}, sku="Style-100")
    await variant(db_session, pid, "Style-100-S", [v["Size", "S"]])
    await db_session.commit()
    await refused(db_session, variant(db_session, pid, "STYLE-100-s", [v["Size", "M"]]))            # a variant against a variant
    await refused(db_session, variant(db_session, pid, "style-100", [v["Size", "M"]]))              # a variant against its own product
    await refused(db_session, new_product(db_session, category, "style-100-S"))                      # a product against a variant
    await refused(db_session, new_product(db_session, category, "STYLE-100"))                        # a product against a product
    await refused(db_session, db_session.execute(text("UPDATE products SET sku = 'RENAMED' WHERE id = :p"), {"p": pid}))
    await refused(db_session, db_session.execute(text("UPDATE product_variants SET sku = 'RENAMED' WHERE product_id = :p"), {"p": pid}))
    # deleting frees the SKU
    other = await new_product(db_session, category, "FREE-ME")
    await db_session.commit()
    await db_session.execute(text("DELETE FROM products WHERE id = :p"), {"p": other})
    await db_session.commit()
    assert await new_product(db_session, category, "free-me")


async def test_two_writers_racing_for_one_sku_leave_exactly_one_winner(test_session_factory, db_session, category):
    p2, v2 = await with_options(db_session, category, {"Size": ["S"]})
    await db_session.commit()
    a, b = test_session_factory(), test_session_factory()
    try:
        await new_product(a, category, "RACE-SKU")                       # A holds the SKU, uncommitted
        task = asyncio.create_task(variant(b, p2, "race-sku", [v2["Size", "S"]]))  # B must wait for A, not slip past it
        await asyncio.sleep(0.4)
        assert not task.done()
        await a.commit()
        with pytest.raises(IntegrityError):
            await task
        await b.rollback()
        count = (await db_session.execute(text("SELECT count(*) FROM sku_registry WHERE sku_norm = 'RACE-SKU'"))).scalar_one()
        assert count == 1
    finally:
        await a.close()
        await b.close()


async def test_two_writers_racing_for_one_combination_leave_exactly_one_winner(test_session_factory, db_session, category):
    pid, v = await with_options(db_session, category, SIZE_COLOUR)
    await db_session.commit()
    a, b = test_session_factory(), test_session_factory()
    combo = [v["Size", "M"], v["Colour", "Blue"]]
    try:
        await variant(a, pid, "COMBO-A", combo)
        task = asyncio.create_task(variant(b, pid, "COMBO-B", combo))
        await asyncio.sleep(0.4)
        assert not task.done()  # serialised on the parent product row / the unique index
        await a.commit()
        with pytest.raises(IntegrityError):
            await task
        await b.rollback()
    finally:
        await a.close()
        await b.close()
    n = (await db_session.execute(text("SELECT count(*) FROM product_variants WHERE product_id = :p"), {"p": pid})).scalar_one()
    assert n == 1


# ============================================================================================== "active needs a variant"
async def test_an_active_product_with_options_needs_an_active_variant(db_session, category):
    pid, v = await with_options(db_session, category, {"Size": ["S", "M"]})
    await db_session.execute(text("UPDATE products SET status = 'active' WHERE id = :p"), {"p": pid})
    await refused(db_session, db_session.commit())                                                 # nothing to sell

    pid, v = await with_options(db_session, category, {"Size": ["S", "M"]})
    only = await variant(db_session, pid, "ACT-S", [v["Size", "S"]], status="inactive")
    await db_session.execute(text("UPDATE products SET status = 'active' WHERE id = :p"), {"p": pid})
    await refused(db_session, db_session.commit())                                                 # the only variant is inactive

    pid, v = await with_options(db_session, category, {"Size": ["S", "M"]})
    only = await variant(db_session, pid, "ACT2-S", [v["Size", "S"]])
    await db_session.execute(text("UPDATE products SET status = 'active' WHERE id = :p"), {"p": pid})
    await db_session.commit()                                                                      # fine: one active variant
    await db_session.execute(text("UPDATE product_variants SET status = 'inactive' WHERE id = :v"), {"v": only})
    await refused(db_session, db_session.commit())                                                 # cannot deactivate the last one
    await db_session.execute(text("DELETE FROM product_variants WHERE id = :v"), {"v": only})
    await refused(db_session, db_session.commit())                                                 # nor delete it


async def test_two_transactions_cannot_each_deactivate_a_different_variant_and_leave_none(test_session_factory, db_session, category):
    """Write skew: with only row-level checks each transaction sees the OTHER variant still active and both commit."""
    pid, v = await with_options(db_session, category, {"Size": ["S", "M"]})
    first = await variant(db_session, pid, "SKEW-S", [v["Size", "S"]])
    second = await variant(db_session, pid, "SKEW-M", [v["Size", "M"]])
    await db_session.execute(text("UPDATE products SET status = 'active' WHERE id = :p"), {"p": pid})
    await db_session.commit()
    a, b = test_session_factory(), test_session_factory()
    try:
        await a.execute(text("UPDATE product_variants SET status = 'inactive' WHERE id = :v"), {"v": first})
        task = asyncio.create_task(b.execute(text("UPDATE product_variants SET status = 'inactive' WHERE id = :v"), {"v": second}))
        await asyncio.sleep(0.4)
        assert not task.done()  # B waits for A (the product row lock) instead of racing it
        await a.commit()
        await task                                  # B's statement now runs against A's committed state ...
        with pytest.raises(IntegrityError):         # ... and its commit finds no active variant left
            await b.commit()
        await b.rollback()
    finally:
        await a.close()
        await b.close()
    active = (await db_session.execute(text("SELECT count(*) FROM product_variants WHERE product_id = :p AND status = 'active'"), {"p": pid})).scalar_one()
    assert active == 1


# ============================================================================================== lines, history and restock events
async def test_cart_and_order_lines_must_match_the_kind_of_product(db_session, category, client, admin_headers):
    option_pid, v = await with_options(db_session, category, {"Size": ["S"]})
    vid = await variant(db_session, option_pid, "LINE-S", [v["Size", "S"]])
    simple = await new_product(db_session, category, stock=5)
    await db_session.commit()
    cart = (await db_session.execute(text("INSERT INTO carts (guest_token) VALUES ('t-lines') RETURNING id"))).scalar_one()
    await db_session.commit()

    def add(product, variant_id):
        return db_session.execute(text("INSERT INTO cart_items (cart_id, product_id, variant_id, quantity, price_cents_snapshot) "
                                       "VALUES (:c, :p, :v, 1, 1000)"), {"c": cart, "p": product, "v": variant_id})

    await refused(db_session, add(option_pid, None))      # a product with options needs a variant
    await refused(db_session, add(simple, vid))           # a simple product has none (and this variant is another product's)
    await refused(db_session, add(simple, 999999))
    await add(option_pid, vid)
    await add(simple, None)
    await db_session.commit()
    await refused(db_session, add(simple, None))          # the same simple line twice: still one line per product
    await refused(db_session, add(option_pid, vid))       # ... and one line per variant

    _h, order, _p = await make_order(client, admin_headers, category)
    await refused(db_session, db_session.execute(text(
        "INSERT INTO order_items (order_id, product_id, product_name_snapshot, price_cents_snapshot, quantity) VALUES (:o, :p, 'x', 1, 1)"),
        {"o": order["id"], "p": option_pid}))


async def test_order_item_details_are_a_historical_record(client, admin_headers, category, db_session):
    limiter.reset()
    _h, order, product = await make_order(client, admin_headers, category, qty=2)
    item = (await db_session.execute(text("SELECT id FROM order_items WHERE order_id = :o"), {"o": order["id"]})).scalar_one()
    for change in ("price_cents_snapshot = 1", "quantity = 1", "product_name_snapshot = 'renamed'", "variant_snapshot = '{}'::jsonb",
                   "sku_snapshot = 'CHANGED'", "sku_snapshot = NULL"):   # a line made now already froze its SKU: it cannot change or be erased
        await refused(db_session, db_session.execute(text(f"UPDATE order_items SET {change} WHERE id = :i"), {"i": item}))
    # A line from before SKU snapshots existed has none: the one-time fill (NULL -> value) is allowed, a second change is not.
    legacy = (await db_session.execute(text(
        "INSERT INTO order_items (order_id, product_id, product_name_snapshot, price_cents_snapshot, quantity) VALUES (:o, :p, 'old', 1, 1) RETURNING id"),
        {"o": order["id"], "p": product["id"]})).scalar_one()
    await db_session.execute(text("UPDATE order_items SET sku_snapshot = 'FIRST', sku_snapshot_source = 'backfill' WHERE id = :i"), {"i": legacy})
    await db_session.commit()
    await refused(db_session, db_session.execute(text("UPDATE order_items SET sku_snapshot = 'SECOND' WHERE id = :i"), {"i": legacy}))


async def test_a_restock_event_is_applied_once_and_never_beyond_the_purchased_quantity(client, admin_headers, category, db_session):
    limiter.reset()
    _h, order, _p = await make_order(client, admin_headers, category, qty=3)
    item = (await db_session.execute(text("SELECT id FROM order_items WHERE order_id = :o"), {"o": order["id"]})).scalar_one()

    async def restock(key, qty):
        return await db_session.execute(text(
            "INSERT INTO order_item_restocks (order_item_id, event_key, quantity) VALUES (:i, :k, :q) ON CONFLICT (order_item_id, event_key) DO NOTHING"),
            {"i": item, "k": key, "q": qty})

    async def restocked():
        return (await db_session.execute(text("SELECT restocked_quantity FROM order_items WHERE id = :i"), {"i": item})).scalar_one()

    assert (await restock("event-1", 2)).rowcount == 1 and await restocked() == 2
    assert (await restock("event-1", 2)).rowcount == 0 and await restocked() == 2          # a replay changes nothing
    await db_session.commit()
    await refused(db_session, restock("event-2", 2))                                          # 2 + 2 > 3 purchased
    assert (await restock("event-2", 1)).rowcount == 1 and await restocked() == 3
    await db_session.commit()
    await refused(db_session, db_session.execute(text(
        "INSERT INTO order_item_restocks (order_item_id, event_key, quantity) VALUES (:i, 'zero', 0)"), {"i": item}))


async def test_a_new_order_line_freezes_the_sku_it_was_bought_under(client, admin_headers, category, db_session):
    limiter.reset()
    _h, order, product = await make_order(client, admin_headers, category, qty=1)
    row = (await db_session.execute(text("SELECT sku_snapshot, sku_snapshot_source, variant_id, restocked_quantity FROM order_items WHERE order_id = :o"), {"o": order["id"]})).one()
    assert tuple(row) == (product["sku"], "order", None, 0)
