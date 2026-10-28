# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""Gap C: admin reports. Every number is checked against figures the test itself controls."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from test_order_payment_lifecycle import API, category, jobs, make_order, paid_order, provider  # noqa: F401

from app.core.config import ConfigurationError, Settings, get_settings
from app.core.rate_limit import limiter

R = f"{API}/admin/reports"
RANGE = {"start": "2026-09-09", "end": "2026-09-14"}


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setattr(get_settings(), "reporting_timezone", "UTC")  # simple day arithmetic; the IST test overrides it


async def set_paid(db, order_id, when: str):
    await db.execute(text("UPDATE order_status_history SET created_at = :t WHERE order_id = :o AND upper(to_status) = 'PAID'"),
                     {"t": datetime.fromisoformat(when), "o": order_id})
    await db.commit()


async def get(client, headers, path, **params):
    r = await client.get(f"{R}/{path}", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def scenario(client, admin_headers, category, provider, db_session, test_session_factory):
    """A (2 x 1500) and B (1 x 2000) paid on 09-10; E (1 x 5000) paid 09-11 then refunded 09-13; C (3 x 700) paid 09-12; D never paid."""
    out = {}
    for key, kw, when in (("A", dict(price=1500, qty=2), "2026-09-10 10:00"), ("B", dict(price=2000, qty=1), "2026-09-10 23:30"),
                          ("E", dict(price=5000, qty=1), "2026-09-11 09:00"), ("C", dict(price=700, qty=3), "2026-09-12 08:00")):
        limiter.reset()  # every order registers a buyer; registration is limited to 5 a minute
        h, o, p, pi = await paid_order(client, admin_headers, category, provider, **kw)
        await set_paid(db_session, o["id"], when)
        out[key] = {"h": h, "o": o, "p": p}
    limiter.reset()
    _, out["D"], _ = await make_order(client, admin_headers, category, price=999, qty=1)  # pending forever: never revenue
    assert (await client.post(f"{API}/payments/{out['E']['o']['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    await db_session.execute(text("UPDATE refunds SET updated_at = '2026-09-13 12:00' WHERE status = 'succeeded'"))
    await db_session.commit()
    return out


# ================================================================================================== access and validation
async def test_reports_are_admin_only_and_read_only(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    for path in ("summary", "sales-by-day", "top-products", "low-stock"):
        assert (await client.get(f"{R}/{path}")).status_code in (401, 403)
        assert (await client.get(f"{R}/{path}", headers=h)).status_code == 403  # a customer
    counts = "SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM payments), (SELECT count(*) FROM refunds), (SELECT count(*) FROM audit_logs)"
    await db_session.rollback()
    before = (await db_session.execute(text(counts))).one()
    for path in ("summary", "sales-by-day", "top-products", "low-stock"):
        await get(client, admin_headers, path)
    await db_session.rollback()
    assert (await db_session.execute(text(counts))).one() == before  # reading changes nothing


@pytest.mark.parametrize(("path", "params"), [
    ("summary", {"start": "2026-09-10", "end": "2026-09-01"}),
    ("sales-by-day", {"start": "2025-01-01", "end": "2026-09-01"}),     # more than 366 days
    ("summary", {"start": "not-a-date"}),
    ("top-products", {"by": "colour"}),
    ("top-products", {"limit": 0}),
    ("top-products", {"limit": 51}),
    ("low-stock", {"threshold": -1}),
    ("low-stock", {"page_size": 101}),
])
async def test_bad_parameters_are_refused(client, admin_headers, path, params):
    assert (await client.get(f"{R}/{path}", headers=admin_headers, params=params)).status_code == 422


async def test_an_inverted_or_oversized_range_gets_a_clear_code(client, admin_headers):
    r = await client.get(f"{R}/summary", headers=admin_headers, params={"start": "2026-09-10", "end": "2026-09-01"})
    assert r.json()["code"] == "invalid_report_range"


# ================================================================================================== the numbers
async def test_sales_by_day_counts_orders_on_the_day_they_were_paid_and_zero_fills(client, admin_headers, category, provider, db_session, test_session_factory):
    s = await scenario(client, admin_headers, category, provider, db_session, test_session_factory)
    body = await get(client, admin_headers, "sales-by-day", **RANGE)
    assert (body["start"], body["end"], body["timezone"], body["currency"], body["minor_unit_exponent"]) == ("2026-09-09", "2026-09-14", "UTC", "usd", 2)
    days = {d["day"]: d for d in body["days"]}
    assert list(days) == [f"2026-09-{n:02d}" for n in range(9, 15)]  # every day, oldest first
    a, b, c, e = (s[k]["o"] for k in "ABCE")
    assert days["2026-09-10"]["orders"] == 2 and days["2026-09-10"]["gross_cents"] == a["total_cents"] + b["total_cents"]
    assert days["2026-09-10"]["units"] == 3 and days["2026-09-10"]["goods_cents"] == 3000 + 2000
    assert days["2026-09-11"]["orders"] == 1 and days["2026-09-11"]["gross_cents"] == e["total_cents"]
    assert days["2026-09-12"]["orders"] == 1 and days["2026-09-12"]["units"] == 3 and days["2026-09-12"]["gross_cents"] == c["total_cents"]
    for quiet in ("2026-09-09", "2026-09-14"):
        assert days[quiet]["orders"] == days[quiet]["gross_cents"] == days[quiet]["refunded_cents"] == days[quiet]["net_cents"] == 0
    # the refund is counted on the day it completed, not the day of the sale
    assert days["2026-09-13"]["refund_count"] == 1 and days["2026-09-13"]["refunded_cents"] == e["total_cents"]
    assert days["2026-09-13"]["net_cents"] == -e["total_cents"] and days["2026-09-11"]["net_cents"] == e["total_cents"]
    for d in body["days"]:
        assert d["gross_cents"] == d["goods_cents"] + d["shipping_cents"] + d["tax_cents"]


async def test_the_summary_totals_match_the_days_and_the_unpaid_order_is_never_revenue(client, admin_headers, category, provider, db_session, test_session_factory):
    s = await scenario(client, admin_headers, category, provider, db_session, test_session_factory)
    daily = await get(client, admin_headers, "sales-by-day", **RANGE)
    summary = await get(client, admin_headers, "summary", **RANGE)
    paid = [s[k]["o"] for k in "ABCE"]
    gross = sum(o["total_cents"] for o in paid)
    assert summary["orders"] == 4 and summary["units"] == 2 + 1 + 3 + 1  # D (pending) is not in either
    assert summary["gross_cents"] == gross == sum(d["gross_cents"] for d in daily["days"])
    assert summary["refund_count"] == 1 and summary["refunded_cents"] == s["E"]["o"]["total_cents"]
    assert summary["net_cents"] == gross - summary["refunded_cents"]
    assert summary["average_order_cents"] == round(gross / 4) and summary["excluded_orders"] == 0
    assert {"sold", "gross", "refunded", "net", "currency", "top_products"} <= set(summary["definitions"])


async def test_a_paid_event_late_in_the_evening_utc_is_the_next_day_in_india(client, admin_headers, category, provider, db_session, monkeypatch):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    await set_paid(db_session, o["id"], "2026-10-01 19:00")  # 00:30 on 2 October in India
    rng = {"start": "2026-09-30", "end": "2026-10-03"}
    monkeypatch.setattr(get_settings(), "reporting_timezone", "UTC")
    utc = {d["day"]: d["orders"] for d in (await get(client, admin_headers, "sales-by-day", **rng))["days"]}
    monkeypatch.setattr(get_settings(), "reporting_timezone", "Asia/Kolkata")
    ist_body = await get(client, admin_headers, "sales-by-day", **rng)
    ist = {d["day"]: d["orders"] for d in ist_body["days"]}
    assert utc["2026-10-01"] == 1 and utc["2026-10-02"] == 0
    assert ist["2026-10-01"] == 0 and ist["2026-10-02"] == 1 and ist_body["timezone"] == "Asia/Kolkata"
    # and the RANGE itself is cut at local midnight: a one-day report for 2 October in India includes it, for 1 October does not
    one = (await get(client, admin_headers, "summary", start="2026-10-02", end="2026-10-02"))["orders"]
    two = (await get(client, admin_headers, "summary", start="2026-10-01", end="2026-10-01"))["orders"]
    assert (one, two) == (1, 0)


async def test_orders_in_another_currency_or_with_none_are_counted_aside_never_added_in(client, admin_headers, category, provider, db_session):
    limiter.reset()
    h1, o1, _, _ = await paid_order(client, admin_headers, category, provider, price=1000)
    h2, o2, _, _ = await paid_order(client, admin_headers, category, provider, price=2000)
    h3, o3, _, _ = await paid_order(client, admin_headers, category, provider, price=4000)
    for o in (o1, o2, o3):
        await set_paid(db_session, o["id"], "2026-09-10 10:00")
    await db_session.execute(text("ALTER TABLE orders DISABLE TRIGGER USER"))
    await db_session.execute(text("UPDATE orders SET currency = 'eur' WHERE id = :o"), {"o": o2["id"]})
    await db_session.execute(text("UPDATE orders SET currency = NULL WHERE id = :o"), {"o": o3["id"]})  # a legacy order
    await db_session.execute(text("ALTER TABLE orders ENABLE TRIGGER USER"))
    await db_session.commit()
    body = await get(client, admin_headers, "summary", **RANGE)
    assert body["orders"] == 1 and body["gross_cents"] == o1["total_cents"] and body["excluded_orders"] == 2
    assert (await get(client, admin_headers, "sales-by-day", **RANGE))["excluded_orders"] == 2


async def test_summary_also_reports_orders_by_status_new_customers_and_open_refunds(client, admin_headers, category, provider, db_session, test_session_factory):
    h, o, p, pi = await paid_order(client, admin_headers, category, provider)
    await make_order(client, admin_headers, category)  # a second customer with a pending order
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202  # requested, not yet sent
    today = datetime.now(UTC).date().isoformat()
    body = await get(client, admin_headers, "summary", start=today, end=today)
    await db_session.rollback()
    direct = dict((await db_session.execute(text("SELECT status::text, count(*) FROM orders GROUP BY 1"))).all())
    assert body["orders_by_status"] == direct and sum(direct.values()) == 2
    assert body["new_customers"] == (await db_session.execute(text("SELECT count(*) FROM users WHERE role::text = 'user'"))).scalar_one() == 2
    assert body["open_refunds"] == 1


async def test_top_products_rank_by_revenue_or_units_exclude_refunded_orders_and_keep_the_snapshot_name(
    client, admin_headers, category, provider, db_session, test_session_factory
):
    s = await scenario(client, admin_headers, category, provider, db_session, test_session_factory)
    a_name, a_sku = s["A"]["p"]["name"], s["A"]["p"]["sku"]
    await client.patch(f"{API}/products/{s['A']['p']['id']}", headers=admin_headers, json={"name": "Renamed Later"})
    by_revenue = (await get(client, admin_headers, "top-products", **RANGE))["items"]
    assert [(i["revenue_cents"], i["units"]) for i in by_revenue] == [(3000, 2), (2100, 3), (2000, 1)]  # A, C, B: E is refunded, D unpaid
    assert by_revenue[0]["name"] == a_name and by_revenue[0]["sku"] == a_sku and by_revenue[0]["current_stock"] == 8  # stock 10 - 2
    by_units = (await get(client, admin_headers, "top-products", by="units", **RANGE))["items"]
    assert [i["units"] for i in by_units] == [3, 2, 1] and (await get(client, admin_headers, "top-products", limit=1, **RANGE))["items"][0]["revenue_cents"] == 3000
    assert (await get(client, admin_headers, "top-products", **RANGE))["by"] == "revenue"


async def test_low_stock_lists_active_products_at_or_below_the_threshold_lowest_first_and_paginates(client, admin_headers, category):
    async def product(sku, stock, status="active"):
        r = await client.post(f"{API}/products", headers=admin_headers, json={
            "name": f"Item {sku}", "description": None, "price_cents": 500, "sku": sku, "stock": stock, "category_id": category, "status": status})
        assert r.status_code == 201, r.text

    for sku, stock, status in (("S-0", 0, "active"), ("S-3", 3, "active"), ("S-10", 10, "active"), ("S-11", 11, "active"), ("S-D", 1, "draft")):
        await product(sku, stock, status)
    body = await get(client, admin_headers, "low-stock")
    assert [(i["sku"], i["stock"]) for i in body["items"]] == [("S-0", 0), ("S-3", 3), ("S-10", 10)] and body["total"] == 3
    assert body["items"][0]["category"] and body["items"][0]["status"] == "active"
    page = await get(client, admin_headers, "low-stock", page_size=2, page=2)
    assert [i["sku"] for i in page["items"]] == ["S-10"] and page["total"] == 3 and page["page"] == 2
    assert [i["sku"] for i in (await get(client, admin_headers, "low-stock", threshold=3))["items"]] == ["S-0", "S-3"]
    everything = await get(client, admin_headers, "low-stock", include_inactive="true")
    assert [i["sku"] for i in everything["items"]] == ["S-0", "S-D", "S-3", "S-10"]


async def test_an_empty_store_reports_zeros_not_errors(client, admin_headers):
    body = await get(client, admin_headers, "summary")
    assert body["orders"] == body["gross_cents"] == body["net_cents"] == body["average_order_cents"] == 0
    assert (await get(client, admin_headers, "top-products"))["items"] == []
    assert (await get(client, admin_headers, "low-stock"))["items"] == []


def test_the_reporting_timezone_must_be_a_real_one():
    base = dict(database_url="postgresql+asyncpg://u:p@db/x", jwt_secret="x", stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    assert Settings(_env_file=None, **base).reporting_timezone == "Asia/Kolkata"
    assert Settings(_env_file=None, reporting_timezone="UTC", **base).reporting_timezone == "UTC"
    for bad in ("Mars/Olympus", "", "IST-ish"):
        with pytest.raises(ConfigurationError):
            Settings(_env_file=None, reporting_timezone=bad, **base)
