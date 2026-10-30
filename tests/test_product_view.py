"""What shoppers and administrators are shown about price and availability (pure functions, no database)."""

from types import SimpleNamespace as NS

import pytest

from app.modules.products.services import product_view as pv


def settings(visibility="availability", threshold=5):
    return NS(public_stock_visibility=visibility, low_stock_display_threshold=threshold)


@pytest.mark.parametrize(("stock", "expected"), [(0, "out_of_stock"), (-1, "out_of_stock"), (1, "low_stock"), (5, "low_stock"), (6, "in_stock"), (500, "in_stock")])
def test_availability_thresholds(stock, expected):
    assert pv.availability(stock, 5) == expected


def test_a_variants_effective_price_is_its_override_else_the_products_price():
    assert pv.effective_price(NS(price_override_cents=700), 1000) == 700
    assert pv.effective_price(NS(price_override_cents=None), 1000) == 1000


def simple(stock=10, price=1000):
    return NS(id=1, option_count=0, price_cents=price, stock=stock)


def test_a_simple_product_shows_availability_not_stock_to_the_public_and_stock_to_admins():
    public = pv.view_fields(simple(10), is_admin=False, detail=False, settings=settings())
    assert public["stock"] is None and public["availability"] == "in_stock" and public["available"] is True
    assert (public["price_cents"], public["price_from_cents"], public["price_to_cents"], public["has_price_range"]) == (1000, 1000, 1000, False)
    assert "base_price_cents" not in public
    admin = pv.view_fields(simple(10), is_admin=True, detail=False, settings=settings())
    assert admin["stock"] == 10 and admin["base_price_cents"] == 1000
    assert pv.view_fields(simple(10), is_admin=False, detail=False, settings=settings("exact"))["stock"] == 10
    assert pv.view_fields(simple(0), is_admin=False, detail=False, settings=settings())["available"] is False
    assert pv.view_fields(simple(3), is_admin=False, detail=False, settings=settings(threshold=2))["availability"] == "in_stock"


def option_product(variants, price=1000):
    product = NS(id=7, option_count=1, price_cents=price)
    rows = [NS(id=i, value1_id=100 + i, value2_id=None, value3_id=None, sku=f"V-{i}", **v) for i, v in enumerate(variants, start=1)]
    option = NS(id=1, name="Size", values=[NS(id=100 + i, value=f"S{i}") for i in range(1, len(variants) + 1)])
    product.variant_rows, product.option_rows = rows, [option]
    return product


class _State:
    unloaded = set()


def test_a_products_price_range_covers_active_variants_including_out_of_stock_ones():
    product = option_product([
        dict(price_override_cents=800, stock=0, status="active"),      # out of stock: still defines the range
        dict(price_override_cents=None, stock=5, status="active"),      # base price
        dict(price_override_cents=2000, stock=9, status="inactive"),    # inactive: not part of it
    ])
    f = pv.view_fields(product, is_admin=False, detail=False, settings=settings())
    assert (f["price_cents"], f["price_from_cents"], f["price_to_cents"], f["has_price_range"]) == (800, 800, 1000, True)
    assert f["variant_count"] == 2 and "variants" not in f
    assert pv.view_fields(product, is_admin=True, detail=False, settings=settings())["variant_count"] == 3  # an administrator counts all


def test_availability_of_a_product_with_options_is_that_of_its_best_active_variant():
    def avail(*stocks, threshold=5):
        product = option_product([dict(price_override_cents=None, stock=s, status="active") for s in stocks])
        return pv.view_fields(product, is_admin=False, detail=False, settings=settings(threshold=threshold))
    assert avail(0, 3, 50)["availability"] == "in_stock"
    assert avail(0, 3)["availability"] == "low_stock" and avail(0, 3)["available"] is True
    assert avail(0, 0)["availability"] == "out_of_stock" and avail(0, 0)["available"] is False
    assert avail(3, threshold=2)["availability"] == "in_stock"
    inactive_only = option_product([dict(price_override_cents=None, stock=50, status="inactive")])
    f = pv.view_fields(inactive_only, is_admin=False, detail=False, settings=settings())
    assert f["available"] is False and f["price_cents"] == 1000          # no active variant: unavailable, base price shown


def test_exact_stock_of_a_product_with_options_is_summed_only_where_allowed():
    product = option_product([dict(price_override_cents=None, stock=4, status="active"), dict(price_override_cents=None, stock=6, status="inactive")])
    assert pv.view_fields(product, is_admin=False, detail=False, settings=settings())["stock"] is None
    assert pv.view_fields(product, is_admin=True, detail=False, settings=settings())["stock"] == 10
    assert pv.view_fields(product, is_admin=False, detail=False, settings=settings("exact"))["stock"] == 4   # public: active variants only


def test_detail_lists_options_and_variants_with_effective_prices_and_hides_inactive_ones_from_the_public():
    product = option_product([dict(price_override_cents=700, stock=0, status="active"), dict(price_override_cents=None, stock=8, status="inactive")])
    public = pv.view_fields(product, is_admin=False, detail=True, settings=settings())
    assert [(o.name, o.values) for o in public["options"]] == [("Size", ["S1", "S2"])]
    assert [(v.sku, v.price_cents, v.price_overridden, v.available, v.stock, v.status) for v in public["variants"]] == [("V-1", 700, True, False, None, None)]
    assert [(o.name, o.value) for o in public["variants"][0].options] == [("Size", "S1")]
    admin = pv.view_fields(product, is_admin=True, detail=True, settings=settings())
    assert [(v.sku, v.price_cents, v.stock, v.status) for v in admin["variants"]] == [("V-1", 700, 0, "active"), ("V-2", 1000, 8, "inactive")]


def test_an_unattached_product_with_options_fails_loudly_instead_of_showing_wrong_data():
    from sqlalchemy import inspect

    product = option_product([dict(price_override_cents=None, stock=1, status="active")])
    real = inspect  # the module asks SQLAlchemy which attributes are unloaded
    pv.sa_inspect = lambda obj: NS(unloaded={"variant_rows"})
    try:
        with pytest.raises(RuntimeError, match="not attached"):
            pv.view_fields(product, is_admin=False, detail=False, settings=settings())
    finally:
        pv.sa_inspect = real
