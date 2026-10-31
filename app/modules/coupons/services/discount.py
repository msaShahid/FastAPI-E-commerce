"""Discount arithmetic. Pure functions on whole paise; no database, no clock."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.modules.tax.gst_calc import _allocate


@dataclass(frozen=True)
class Discount:
    goods_cents: int        # taken off the merchandise (0 for a free-shipping coupon)
    shipping_cents: int     # taken off the shipping charge (0 unless free-shipping)


def compute_discount(*, kind: str, percent_off: Decimal | None, amount_off_cents: int | None, max_discount_cents: int | None,
                     subtotal_cents: int, shipping_cents: int) -> Discount:
    """What a coupon takes off: never negative and never more than what it applies to."""
    if kind == "percentage":
        raw = int((Decimal(subtotal_cents) * percent_off / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        if max_discount_cents is not None:
            raw = min(raw, max_discount_cents)
        return Discount(min(raw, subtotal_cents), 0)
    if kind == "fixed":
        return Discount(min(amount_off_cents, subtotal_cents), 0)
    if kind == "free_shipping":
        return Discount(0, shipping_cents)
    raise ValueError(f"unknown coupon kind {kind!r}")


def share_discount(discount_cents: int, line_values: list[int]) -> list[int]:
    """Spreads a discount over the order lines in proportion to their value, exactly (the shares add up to the discount, and no line is
    discounted by more than it costs)."""
    return _allocate(discount_cents, line_values)
