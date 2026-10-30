from datetime import date

from pydantic import BaseModel


class ReportPeriod(BaseModel):
    """Every money figure in a report is an integer in the MINOR units of `currency` (paise for INR): divide by
    10**minor_unit_exponent to display. `start` and `end` are inclusive calendar days in `timezone`."""

    start: date
    end: date
    timezone: str
    currency: str
    minor_unit_exponent: int


class DayRow(BaseModel):
    day: date
    orders: int
    units: int
    gross_cents: int          # what customers paid: goods + shipping + tax
    goods_cents: int
    shipping_cents: int
    tax_cents: int
    refund_count: int
    refunded_cents: int
    net_cents: int            # gross - refunded, both counted on THIS day


class SalesByDay(ReportPeriod):
    days: list[DayRow]        # every day in the range, zero-filled, oldest first
    excluded_orders: int      # paid orders in another currency or with none recorded: never added into these totals


class Summary(ReportPeriod):
    orders: int
    units: int
    gross_cents: int
    goods_cents: int
    shipping_cents: int
    tax_cents: int
    refund_count: int
    refunded_cents: int
    net_cents: int
    average_order_cents: int
    orders_by_status: dict[str, int]   # orders CREATED in the period, by their status now
    new_customers: int
    open_refunds: int                  # right now, whatever the period: requested, pending or uncertain
    excluded_orders: int
    definitions: dict[str, str]


class TopProduct(BaseModel):
    product_id: int | None
    sku: str | None
    name: str                          # the name on the orders (a snapshot), not necessarily today's
    units: int
    orders: int
    revenue_cents: int                 # goods only (price x quantity), excluding shipping and tax
    current_stock: int | None
    variant_id: int | None = None      # set when grouped by variant
    variant_label: str | None = None   # e.g. "Size: M / Colour: Blue", as the order recorded it


class TopProducts(ReportPeriod):
    by: str
    group_by: str = "product"          # "product" (all variants of a product together) or "variant"
    items: list[TopProduct]


class LowStockItem(BaseModel):
    id: int
    sku: str
    name: str
    category: str | None
    stock: int
    price_cents: int                   # the price this unit sells at (a variant's effective price)
    status: str
    variant_id: int | None = None      # set for a variant; the product's id and name are still in id and name
    options: str | None = None         # e.g. "Size: M / Colour: Blue"
