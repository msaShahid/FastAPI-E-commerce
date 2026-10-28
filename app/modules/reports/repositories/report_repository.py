"""Read-only aggregate queries. Plain SQL with bound parameters (never string-built from input). Times in the database are naive
UTC; day buckets are converted to the reporting timezone in SQL.

An order counts as SOLD on the day it first became PAID (its status-history row), not the day it was placed: an unpaid, expired or
abandoned order is never revenue. An order can be paid only once, so the range filter is applied inside the CTE."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

FIRST_PAID = """
WITH first_paid AS (
    SELECT DISTINCT ON (h.order_id) h.order_id, h.created_at AS paid_at
    FROM order_status_history h
    WHERE upper(h.to_status) = 'PAID' AND h.created_at >= :s AND h.created_at < :e
    ORDER BY h.order_id, h.created_at
)
"""
LOCAL_DAY = "((%s AT TIME ZONE 'UTC') AT TIME ZONE CAST(:tz AS text))::date"


class ReportRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _rows(self, sql: str, **params):
        return (await self.db.execute(text(sql), params)).all()

    async def sales_by_day(self, s: datetime, e: datetime, tz: str, cur: str):
        return await self._rows(
            FIRST_PAID + f"""
            SELECT {LOCAL_DAY % 'p.paid_at'} AS day, count(*) AS orders, coalesce(sum(o.total_cents), 0) AS gross,
                   coalesce(sum(o.subtotal_cents), 0) AS goods, coalesce(sum(o.shipping_cents), 0) AS shipping,
                   coalesce(sum(o.tax_cents), 0) AS tax
            FROM first_paid p JOIN orders o ON o.id = p.order_id
            WHERE o.currency = :cur GROUP BY 1""",
            s=s, e=e, tz=tz, cur=cur,
        )

    async def units_by_day(self, s: datetime, e: datetime, tz: str, cur: str):
        return await self._rows(
            FIRST_PAID + f"""
            SELECT {LOCAL_DAY % 'p.paid_at'} AS day, coalesce(sum(oi.quantity), 0) AS units
            FROM first_paid p JOIN orders o ON o.id = p.order_id JOIN order_items oi ON oi.order_id = o.id
            WHERE o.currency = :cur GROUP BY 1""",
            s=s, e=e, tz=tz, cur=cur,
        )

    async def refunds_by_day(self, s: datetime, e: datetime, tz: str, cur: str):
        return await self._rows(
            f"""SELECT {LOCAL_DAY % 'r.updated_at'} AS day, count(*) AS n, coalesce(sum(r.amount_cents), 0) AS amount
            FROM refunds r WHERE r.status = 'succeeded' AND r.currency = :cur AND r.updated_at >= :s AND r.updated_at < :e GROUP BY 1""",
            s=s, e=e, tz=tz, cur=cur,
        )

    async def excluded_orders(self, s: datetime, e: datetime, cur: str) -> int:
        row = await self._rows(
            FIRST_PAID + "SELECT count(*) FROM first_paid p JOIN orders o ON o.id = p.order_id WHERE o.currency IS DISTINCT FROM :cur",
            s=s, e=e, cur=cur,
        )
        return row[0][0]

    async def orders_by_status(self, s: datetime, e: datetime):
        return await self._rows("SELECT status::text, count(*) FROM orders WHERE created_at >= :s AND created_at < :e GROUP BY 1", s=s, e=e)

    async def new_customers(self, s: datetime, e: datetime) -> int:
        return (await self._rows("SELECT count(*) FROM users WHERE role::text = 'user' AND created_at >= :s AND created_at < :e", s=s, e=e))[0][0]

    async def open_refunds(self) -> int:
        return (await self._rows("SELECT count(*) FROM refunds WHERE status IN ('requested', 'pending', 'uncertain')"))[0][0]

    async def top_products(self, s: datetime, e: datetime, cur: str, by: str, limit: int):
        order = "revenue DESC, units DESC" if by == "revenue" else "units DESC, revenue DESC"
        return await self._rows(
            FIRST_PAID + f"""
            SELECT oi.product_id, max(oi.product_name_snapshot) AS name, sum(oi.quantity) AS units,
                   count(DISTINCT oi.order_id) AS orders, sum(oi.price_cents_snapshot * oi.quantity) AS revenue,
                   max(pr.sku) AS sku, max(pr.stock) AS stock
            FROM first_paid p
            JOIN orders o ON o.id = p.order_id
            JOIN order_items oi ON oi.order_id = o.id
            LEFT JOIN products pr ON pr.id = oi.product_id
            WHERE o.currency = :cur AND o.status::text NOT IN ('refunded', 'cancelled')
            GROUP BY oi.product_id, CASE WHEN oi.product_id IS NULL THEN oi.product_name_snapshot END
            ORDER BY {order}, name LIMIT :n""",  # `order` is one of two fixed strings chosen above, never user text
            s=s, e=e, cur=cur, n=limit,
        )

    async def low_stock(self, threshold: int, include_inactive: bool, offset: int, limit: int):
        where = "pr.stock <= :t" + ("" if include_inactive else " AND pr.status::text = 'active'")
        total = (await self._rows(f"SELECT count(*) FROM products pr WHERE {where}", t=threshold))[0][0]
        rows = await self._rows(
            f"""SELECT pr.id, pr.sku, pr.name, c.name AS category, pr.stock, pr.price_cents, pr.status::text
            FROM products pr LEFT JOIN categories c ON c.id = pr.category_id WHERE {where}
            ORDER BY pr.stock ASC, pr.name ASC, pr.id ASC OFFSET :o LIMIT :l""",
            t=threshold, o=offset, l=limit,
        )
        return total, rows
