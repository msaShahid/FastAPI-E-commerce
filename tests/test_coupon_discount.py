"""The discount arithmetic: what each kind of coupon takes off, and how a discount is shared over the lines."""

import random
from decimal import Decimal as D

import pytest

from app.modules.coupons.services.discount import compute_discount, share_discount


def pct(percent, subtotal, cap=None, shipping=0):
    return compute_discount(kind="percentage", percent_off=D(percent), amount_off_cents=None, max_discount_cents=cap, subtotal_cents=subtotal, shipping_cents=shipping)


def test_a_percentage_takes_that_share_of_the_goods_rounded_half_up():
    assert pct("10", 100000).goods_cents == 10000
    assert pct("12.5", 999).goods_cents == 125          # 124.875 -> 125
    assert pct("10", 1005).goods_cents == 101           # 100.5 -> 101 (half rounds up)
    assert pct("0.01", 100).goods_cents == 0 and pct("100", 4321).goods_cents == 4321


def test_a_percentage_is_capped_when_a_cap_is_set_and_never_exceeds_the_goods():
    assert pct("50", 100000, cap=2000).goods_cents == 2000
    assert pct("50", 100000, cap=999999).goods_cents == 50000
    assert pct("100", 500, cap=900).goods_cents == 500


def test_a_fixed_amount_never_takes_more_than_the_goods_cost():
    fixed = lambda amount, subtotal: compute_discount(kind="fixed", percent_off=None, amount_off_cents=amount, max_discount_cents=None, subtotal_cents=subtotal, shipping_cents=0)  # noqa: E731
    assert fixed(5000, 20000).goods_cents == 5000 and fixed(5000, 3000).goods_cents == 3000 and fixed(1, 0).goods_cents == 0


def test_a_free_shipping_coupon_waives_the_shipping_and_leaves_the_goods_alone():
    d = compute_discount(kind="free_shipping", percent_off=None, amount_off_cents=None, max_discount_cents=None, subtotal_cents=20000, shipping_cents=4900)
    assert (d.goods_cents, d.shipping_cents) == (0, 4900)


def test_an_unknown_kind_is_refused_not_ignored():
    with pytest.raises(ValueError):
        compute_discount(kind="bogus", percent_off=None, amount_off_cents=None, max_discount_cents=None, subtotal_cents=1, shipping_cents=0)


def test_a_discount_is_shared_over_the_lines_exactly_and_never_beyond_a_lines_value():
    assert share_discount(1000, [60000, 40000]) == [600, 400]
    assert share_discount(1, [500, 500]) == [1, 0]
    assert share_discount(0, [100, 200]) == [0, 0]
    assert share_discount(7, [1, 1, 1]) == [3, 2, 2]


@pytest.mark.parametrize("seed", range(200))
def test_shares_always_add_up_and_stay_within_each_line(seed):
    rng = random.Random(seed)
    values = [rng.randint(1, 500000) for _ in range(rng.randint(1, 8))]
    discount = rng.randint(0, sum(values))
    shares = share_discount(discount, values)
    assert sum(shares) == discount and all(0 <= s <= v for s, v in zip(shares, values, strict=True))
