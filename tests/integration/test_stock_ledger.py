# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""E1: every stock change is recorded, atomically, with before/after values; admin changes need a reason; the ledger cannot be rewritten."""

import asyncio

from sqlalchemy import text
from test_order_payment_lifecycle import API, category, jobs, make_order, paid_order, provider  # noqa: F401
from test_variant_schema import refused, variant, with_options

from app.core.rate_limit import limiter

COLUMNS = "kind, delta, before_qty, after_qty, actor_user_id, reason, order_id, variant_id, product_id"


async def ledger(db, product_id=None, variant_id=None):
    await db.rollback()
    where, params = ("WHERE variant_id = :v", {"v": variant_id}) if variant_id else ("WHERE product_id = :p AND variant_id IS NULL", {"p": product_id})
    return (await db.execute(text(f"SELECT {COLUMNS} FROM stock_movements {where} ORDER BY id"), params)).all()


async def create_product(client, admin_headers, category, stock, sku):
    r = await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"Ledger {sku}", "description": None, "price_cents": 1000, "sku": sku, "stock": stock, "category_id": category, "status": "active"})
    assert r.status_code == 201, r.text
    return r.json()


async def assert_balanced(db):
    """The ledger explains every unit: for each product (and variant), the sum of its movements equals its stock."""
    await db.rollback()
    off_products = (await db.execute(text(
        "SELECT p.id FROM products p LEFT JOIN (SELECT product_id, sum(delta) s FROM stock_movements WHERE variant_id IS NULL GROUP BY 1) m "
        "ON m.product_id = p.id WHERE p.stock <> coalesce(m.s, 0)"))).all()
    off_variants = (await db.execute(text(
        "SELECT v.id FROM product_variants v LEFT JOIN (SELECT variant_id, sum(delta) s FROM stock_movements WHERE variant_id IS NOT NULL GROUP BY 1) m "
        "ON m.variant_id = v.id WHERE v.stock <> coalesce(m.s, 0)"))).all()
    assert off_products == [] and off_variants == []
    assert (await db.execute(text("SELECT count(*) FROM stock_movements WHERE kind = 'unspecified'"))).scalar_one() == 0


# ============================================================================================== what the application records
async def test_a_new_products_opening_stock_is_recorded_and_zero_stock_is_not(client, admin_headers, category, db_session):
    p = await create_product(client, admin_headers, category, 7, "LDG-1")
    z = await create_product(client, admin_headers, category, 0, "LDG-2")
    assert [(r.kind, r.delta, r.before_qty, r.after_qty) for r in await ledger(db_session, p["id"])] == [("initial", 7, 0, 7)]
    assert await ledger(db_session, z["id"]) == []


async def test_sales_and_restocks_are_recorded_with_the_order_and_the_ledger_balances(client, admin_headers, category, provider, db_session, test_session_factory):
    limiter.reset()
    h, order, product = await make_order(client, admin_headers, category, price=1000, stock=10, qty=3)  # a sale of 3
    assert [(r.kind, r.delta, r.before_qty, r.after_qty) for r in await ledger(db_session, product["id"])] == [("initial", 10, 0, 10), ("sale", -3, 10, 7)]
    assert (await client.post(f"{API}/orders/{order['id']}/cancel", headers=h)).status_code == 200  # an unpaid order is cancelled: stock comes back
    rows = await ledger(db_session, product["id"])
    assert [(r.kind, r.delta, r.after_qty) for r in rows][-1] == ("restock", 3, 10) and str(rows[-1].order_id) == order["id"]

    limiter.reset()
    _h, paid, p2, _pi = await paid_order(client, admin_headers, category, provider, price=1000, stock=10, qty=2)
    assert (await client.post(f"{API}/admin/orders/{paid['id']}/cancel-and-refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)  # the provider confirms the refund: only now is stock released
    rows = await ledger(db_session, p2["id"])
    assert [(r.kind, r.delta) for r in rows] == [("initial", 10), ("sale", -2), ("restock", 2)]

    limiter.reset()
    _h, shipped, p3, _pi = await paid_order(client, admin_headers, category, provider, price=1000, stock=10, qty=2)
    assert (await client.patch(f"{API}/orders/{shipped['id']}/status", json={"status": "fulfilled"}, headers=admin_headers)).status_code == 200
    assert (await client.post(f"{API}/payments/{shipped['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert [r.kind for r in await ledger(db_session, p3["id"])] == ["initial", "sale"]  # a refund of a SHIPPED order does not restock (policy unchanged)
    await assert_balanced(db_session)


async def test_an_admins_stock_change_needs_a_reason_and_records_who_why_and_before_after(client, admin_headers, category, db_session):
    p = await create_product(client, admin_headers, category, 10, "LDG-3")
    me = (await client.get(f"{API}/auth/me", headers=admin_headers)).json()["id"]
    url = f"{API}/products/{p['id']}"
    for body in ({"stock": 25}, {"stock": 25, "stock_reason": "ab"}, {"stock": 25, "stock_reason": "   "}):
        r = await client.patch(url, headers=admin_headers, json=body)
        assert r.status_code == 422 and r.json()["code"] == "stock_reason_required"
    assert (await client.get(url, headers=admin_headers)).json()["stock"] == 10                      # nothing changed
    ok = await client.patch(url, headers=admin_headers, json={"stock": 25, "stock_reason": "annual recount"})
    assert ok.status_code == 200 and ok.json()["stock"] == 25
    rows = await ledger(db_session, p["id"])
    last = rows[-1]
    assert (last.kind, last.delta, last.before_qty, last.after_qty, last.reason) == ("admin_set", 15, 10, 25, "annual recount") and str(last.actor_user_id) == me
    n = len(rows)
    await client.patch(url, headers=admin_headers, json={"stock": 25, "stock_reason": "same number again"})  # no change: nothing to record
    await client.patch(url, headers=admin_headers, json={"price_cents": 1500})                              # no stock: no reason needed, no row
    assert len(await ledger(db_session, p["id"])) == n
    await assert_balanced(db_session)


# ============================================================================================== what the database enforces
async def test_the_ledger_is_append_only_and_cannot_be_written_by_hand(client, admin_headers, category, db_session):
    p = await create_product(client, admin_headers, category, 5, "LDG-4")
    await db_session.rollback()
    await refused(db_session, db_session.execute(text("UPDATE stock_movements SET reason = 'edited' WHERE product_id = :p"), {"p": p["id"]}))
    await refused(db_session, db_session.execute(text("DELETE FROM stock_movements WHERE product_id = :p"), {"p": p["id"]}))
    await refused(db_session, db_session.execute(text(
        "INSERT INTO stock_movements (kind, product_id, delta, before_qty, after_qty) VALUES ('sale', :p, -1, 5, 4)"), {"p": p["id"]}))  # forged
    assert len(await ledger(db_session, p["id"])) == 1


async def test_an_admin_change_without_who_and_why_is_refused_by_the_database_itself(client, admin_headers, category, db_session):
    p = await create_product(client, admin_headers, category, 5, "LDG-5")
    await db_session.rollback()
    await db_session.execute(text("SELECT set_config('novacart.stock_kind', 'admin_set', true)"))
    await refused(db_session, db_session.execute(text("UPDATE products SET stock = 9 WHERE id = :p"), {"p": p["id"]}))
    assert (await db_session.execute(text("SELECT stock FROM products WHERE id = :p"), {"p": p["id"]})).scalar_one() == 5


async def test_a_stock_change_and_its_ledger_row_commit_or_roll_back_together(client, admin_headers, category, db_session):
    p = await create_product(client, admin_headers, category, 5, "LDG-6")
    await db_session.rollback()
    await db_session.execute(text("SELECT set_config('novacart.stock_kind', 'sale', true)"))
    await db_session.execute(text("UPDATE products SET stock = 1 WHERE id = :p"), {"p": p["id"]})
    await db_session.rollback()
    assert len(await ledger(db_session, p["id"])) == 1
    assert (await db_session.execute(text("SELECT stock FROM products WHERE id = :p"), {"p": p["id"]})).scalar_one() == 5


async def test_concurrent_changes_keep_before_after_exact_and_never_go_negative(client, admin_headers, category, test_session_factory, db_session):
    p = await create_product(client, admin_headers, category, 3, "LDG-7")

    async def buy():
        async with test_session_factory() as s:
            await s.execute(text("SELECT set_config('novacart.stock_kind', 'sale', true)"))
            won = (await s.execute(text("UPDATE products SET stock = stock - 1 WHERE id = :p AND stock >= 1"), {"p": p["id"]})).rowcount
            await s.commit()
            return won

    results = await asyncio.gather(*[buy() for _ in range(10)])
    assert sum(results) == 3                                                    # ten buyers, three units: three sales, no oversell
    assert (await db_session.execute(text("SELECT stock FROM products WHERE id = :p"), {"p": p["id"]})).scalar_one() == 0

    async def restock():
        async with test_session_factory() as s:
            await s.execute(text("SELECT set_config('novacart.stock_kind', 'restock', true)"))
            await s.execute(text("UPDATE products SET stock = stock + 2 WHERE id = :p"), {"p": p["id"]})
            await s.commit()

    await asyncio.gather(*[restock() for _ in range(8)])
    rows = await ledger(db_session, p["id"])
    assert (await db_session.execute(text("SELECT stock FROM products WHERE id = :p"), {"p": p["id"]})).scalar_one() == 16
    for previous, current in zip(rows, rows[1:], strict=False):
        assert current.before_qty == previous.after_qty                         # an unbroken chain: no two changes saw the same "before"
    assert all(r.after_qty >= 0 for r in rows) and sum(r.delta for r in rows) == 16
    await assert_balanced(db_session)


async def test_variant_stock_is_recorded_against_its_variant_and_balances(category, db_session):
    from test_variant_schema import SIZE_COLOUR

    pid, v = await with_options(db_session, category, SIZE_COLOUR)
    vid = await variant(db_session, pid, "LV-1", [v["Size", "S"], v["Colour", "Black"]], stock=4)
    await db_session.execute(text("SELECT set_config('novacart.stock_kind', 'sale', true)"))
    await db_session.execute(text("UPDATE product_variants SET stock = stock - 1 WHERE id = :v"), {"v": vid})
    await db_session.commit()
    rows = await ledger(db_session, variant_id=vid)
    assert [(r.kind, r.delta, r.before_qty, r.after_qty, r.product_id) for r in rows] == [("initial", 4, 0, 4, pid), ("sale", -1, 4, 3, pid)]
    assert await ledger(db_session, pid) == []          # the PRODUCT itself holds no stock
    await assert_balanced(db_session)
