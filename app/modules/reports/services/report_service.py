from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from app.core.config import get_settings
from app.core.money import get_currency
from app.modules.reports.exceptions.report_exceptions import (
    InvalidReportRangeError,
    ReportCurrencyUnknownError,
)
from app.modules.reports.repositories.report_repository import ReportRepository
from app.modules.reports.schemas.reports import (
    DayRow,
    LowStockItem,
    SalesByDay,
    Summary,
    TopProduct,
    TopProducts,
)
from app.modules.store.repositories.store_repository import StoreRepository

MAX_RANGE_DAYS = 366

DEFINITIONS = {
    "sold": "An order counts on the day it first became PAID (in the reporting timezone), not the day it was placed. Orders that were "
            "never paid (pending, expired, cancelled before payment) are never revenue.",
    "gross": "What customers paid for the orders sold in the period: goods + shipping + tax.",
    "refunded": "Refunds that SUCCEEDED in the period, counted on the day they completed, whichever day the order was sold.",
    "net": "gross - refunded.",
    "currency": "Only orders in the store currency are added up. A paid order in another currency, or with none recorded, is counted in "
                "`excluded_orders` and never mixed in.",
    "top_products": "Goods revenue (price x quantity) of orders sold in the period that are not currently refunded or cancelled. A partial "
                    "refund made outside NovaCart cannot be assigned to items and is not deducted.",
}


class ReportService:
    def __init__(self, repository: ReportRepository, store: StoreRepository) -> None:
        self.repo = repository
        self.store = store

    # ------------------------------------------------------------------------------------------------ common
    async def _period(self, start: date | None, end: date | None) -> dict:
        settings = get_settings()
        zone = ZoneInfo(settings.reporting_timezone)
        today = datetime.now(UTC).astimezone(zone).date()
        end = end or today
        start = start or (end - timedelta(days=29))
        if end < start:
            raise InvalidReportRangeError("`end` must not be before `start`")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise InvalidReportRangeError(f"A report covers at most {MAX_RANGE_DAYS} days")
        code, _ = await self.store.effective_currency(settings.default_store_currency)
        if code is None:
            raise ReportCurrencyUnknownError()
        currency = get_currency(code)
        # Local midnight to local midnight, expressed in the database's naive UTC.
        s = datetime.combine(start, time.min, zone).astimezone(UTC).replace(tzinfo=None)
        e = datetime.combine(end + timedelta(days=1), time.min, zone).astimezone(UTC).replace(tzinfo=None)
        return {"start": start, "end": end, "tz": settings.reporting_timezone, "cur": currency.code, "exp": currency.exponent, "s": s, "e": e}

    @staticmethod
    def _header(p: dict) -> dict:
        return {"start": p["start"], "end": p["end"], "timezone": p["tz"], "currency": p["cur"], "minor_unit_exponent": p["exp"]}

    async def _days(self, p: dict) -> list[DayRow]:
        sales = {r[0]: r for r in await self.repo.sales_by_day(p["s"], p["e"], p["tz"], p["cur"])}
        units = {r[0]: r[1] for r in await self.repo.units_by_day(p["s"], p["e"], p["tz"], p["cur"])}
        refunds = {r[0]: r for r in await self.repo.refunds_by_day(p["s"], p["e"], p["tz"], p["cur"])}
        rows, day = [], p["start"]
        while day <= p["end"]:
            sale, refund = sales.get(day), refunds.get(day)
            gross = int(sale[2]) if sale else 0
            refunded = int(refund[2]) if refund else 0
            rows.append(DayRow(
                day=day, orders=int(sale[1]) if sale else 0, units=int(units.get(day, 0)), gross_cents=gross,
                goods_cents=int(sale[3]) if sale else 0, shipping_cents=int(sale[4]) if sale else 0, tax_cents=int(sale[5]) if sale else 0,
                refund_count=int(refund[1]) if refund else 0, refunded_cents=refunded, net_cents=gross - refunded,
            ))
            day += timedelta(days=1)
        return rows

    # ------------------------------------------------------------------------------------------------ reports
    async def sales_by_day(self, start: date | None, end: date | None) -> SalesByDay:
        p = await self._period(start, end)
        return SalesByDay(**self._header(p), days=await self._days(p), excluded_orders=await self.repo.excluded_orders(p["s"], p["e"], p["cur"]))

    async def summary(self, start: date | None, end: date | None) -> Summary:
        p = await self._period(start, end)
        days = await self._days(p)
        total = lambda f: sum(getattr(d, f) for d in days)  # noqa: E731
        orders = total("orders")
        average = int((Decimal(total("gross_cents")) / orders).quantize(Decimal(1), rounding=ROUND_HALF_UP)) if orders else 0
        return Summary(
            **self._header(p), orders=orders, units=total("units"), gross_cents=total("gross_cents"), goods_cents=total("goods_cents"),
            shipping_cents=total("shipping_cents"), tax_cents=total("tax_cents"), refund_count=total("refund_count"),
            refunded_cents=total("refunded_cents"), net_cents=total("net_cents"), average_order_cents=average,
            orders_by_status={status: int(n) for status, n in await self.repo.orders_by_status(p["s"], p["e"])},
            new_customers=int(await self.repo.new_customers(p["s"], p["e"])), open_refunds=int(await self.repo.open_refunds()),
            excluded_orders=await self.repo.excluded_orders(p["s"], p["e"], p["cur"]), definitions=DEFINITIONS,
        )

    async def top_products(self, start: date | None, end: date | None, by: str, limit: int) -> TopProducts:
        p = await self._period(start, end)
        rows = await self.repo.top_products(p["s"], p["e"], p["cur"], by, limit)
        return TopProducts(**self._header(p), by=by, items=[
            TopProduct(product_id=r[0], name=r[1], units=int(r[2]), orders=int(r[3]), revenue_cents=int(r[4]), sku=r[5],
                       current_stock=None if r[6] is None else int(r[6]))
            for r in rows
        ])

    async def low_stock(self, threshold: int, include_inactive: bool, offset: int, limit: int):
        total, rows = await self.repo.low_stock(threshold, include_inactive, offset, limit)
        return total, [LowStockItem(id=r[0], sku=r[1], name=r[2], category=r[3], stock=r[4], price_cents=r[5], status=r[6]) for r in rows]
