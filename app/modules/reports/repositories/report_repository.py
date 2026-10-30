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
        """The units that are SOLD: simple products, and the variants of products with options. A product with options holds no stock of its
        own, so it is never listed itself (its 0 would be meaningless)."""
        simple_active = "" if include_inactive else " AND pr.status::text = 'active'"
        variant_active = "" if include_inactive else " AND pr.status::text = 'active' AND v.status = 'active'"
        units = f"""
            SELECT pr.id AS product_id, NULL::integer AS variant_id, pr.sku AS sku, pr.name AS name, pr.category_id AS category_id, pr.stock AS stock,
                   pr.price_cents AS price_cents, pr.status::text AS status, NULL::text AS options
            FROM products pr WHERE pr.option_count = 0{simple_active}
            UNION ALL
            SELECT pr.id, v.id, v.sku, pr.name, pr.category_id, v.stock, coalesce(v.price_override_cents, pr.price_cents),
                   CASE WHEN pr.status::text <> 'active' THEN pr.status::text ELSE v.status END,
                   concat_ws(' / ', o1.name || ': ' || ov1.value, o2.name || ': ' || ov2.value, o3.name || ': ' || ov3.value)
            FROM product_variants v JOIN products pr ON pr.id = v.product_id
            LEFT JOIN product_option_values ov1 ON ov1.id = v.value1_id LEFT JOIN product_options o1 ON o1.id = ov1.option_id
            LEFT JOIN product_option_values ov2 ON ov2.id = v.value2_id LEFT JOIN product_options o2 ON o2.id = ov2.option_id
            LEFT JOIN product_option_values ov3 ON ov3.id = v.value3_id LEFT JOIN product_options o3 ON o3.id = ov3.option_id
            WHERE true{variant_active}"""
        total = (await self._rows(f"WITH units AS ({units}) SELECT count(*) FROM units WHERE stock <= :t", t=threshold))[0][0]
        rows = await self._rows(
            f"""WITH units AS ({units})
            SELECT u.product_id, u.sku, u.name, c.name AS category, u.stock, u.price_cents, u.status, u.variant_id, u.options
            FROM units u LEFT JOIN categories c ON c.id = u.category_id WHERE u.stock <= :t
            ORDER BY u.stock ASC, u.name ASC, u.product_id ASC, u.variant_id ASC NULLS FIRST OFFSET :o LIMIT :l""",
            t=threshold, o=offset, l=limit,
        )
        return total, rows

    async def top_variants(self, s: datetime, e: datetime, cur: str, by: str, limit: int):
        """Like top_products, but one row per VARIANT sold (a simple product is its own row)."""
        order = "revenue DESC, units DESC" if by == "revenue" else "units DESC, revenue DESC"
        return await self._rows(
            FIRST_PAID + f"""
            SELECT oi.product_id, max(oi.product_name_snapshot) AS name, sum(oi.quantity) AS units, count(DISTINCT oi.order_id) AS orders,
                   sum(oi.price_cents_snapshot * oi.quantity) AS revenue, max(oi.sku_snapshot) AS sku,
                   coalesce(max(v.stock), max(pr.stock)) AS stock, oi.variant_id,
                   (array_agg(oi.variant_snapshot ORDER BY oi.id DESC))[1] AS snapshot
            FROM first_paid p
            JOIN orders o ON o.id = p.order_id
            JOIN order_items oi ON oi.order_id = o.id
            LEFT JOIN products pr ON pr.id = oi.product_id
            LEFT JOIN product_variants v ON v.id = oi.variant_id
            WHERE o.currency = :cur AND o.status::text NOT IN ('refunded', 'cancelled')
            GROUP BY oi.product_id, oi.variant_id
            ORDER BY {order}, name, oi.variant_id NULLS FIRST LIMIT :n""",  # `order` is one of two fixed strings chosen above, never user text
            s=s, e=e, cur=cur, n=limit,
        )
