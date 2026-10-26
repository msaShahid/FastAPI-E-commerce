"""The demo dataset (scripts/seed_data.py) must be internally consistent, or the seed fails half-way through a client demo."""

import re
from collections import Counter

from scripts import seed_data as d


def test_skus_and_order_keys_are_unique_and_well_formed():
    skus = [p.sku for p in d.PRODUCTS]
    assert len(skus) == len(set(skus)) and all(re.fullmatch(r"[A-Z0-9-]+", s) for s in skus)
    keys = [o.key for o in d.ORDERS]
    assert len(keys) == len(set(keys)) and all(k.startswith("demo-seed-") for k in keys)  # the seed recognises its own orders by this


def test_every_product_has_a_known_category_and_a_known_tax_heading_and_a_sensible_price():
    categories = {name for name, _desc, _hue in d.CATEGORIES}
    for p in d.PRODUCTS:
        assert p.category in categories, p.sku
        assert p.hsn is None or p.hsn in d.HSN, p.sku
        assert 0 < p.rupees < 100_000 and p.paise == p.rupees * 100, p.sku
        assert p.status in ("active", "draft", "archived") and p.stock >= 0 and p.description.strip(), p.sku
    assert all(re.fullmatch(r"[0-9]{2}|[0-9]{4}|[0-9]{6}|[0-9]{8}", code) for code in d.HSN)  # the API accepts these lengths only


def test_orders_and_carts_only_reference_real_active_products_and_customers_and_never_exceed_stock():
    products = {p.sku: p for p in d.PRODUCTS}
    wanted = Counter()
    for plan in d.ORDERS:
        assert plan.customer is None or 0 <= plan.customer < len(d.CUSTOMERS)
        assert plan.outcome in {"fulfilled", "paid", "refunded", "cancel_and_refund", "customer_cancelled", "guest_paid"}
        for sku, qty in plan.items:
            assert products[sku].status == "active" and qty > 0, (plan.key, sku)
            wanted[sku] += qty
    for _customer, items in d.CARTS:
        for sku, qty in items:
            assert products[sku].status == "active" and products[sku].stock > 0 and qty > 0, sku  # a cart can't hold an unsellable product
    for sku, qty in wanted.items():
        assert products[sku].stock >= qty, f"{sku}: orders want {qty}, stock is {products[sku].stock}"


def test_the_demo_covers_every_order_outcome_and_a_guest():
    outcomes = {o.outcome for o in d.ORDERS}
    assert {"fulfilled", "paid", "refunded", "cancel_and_refund", "customer_cancelled", "guest_paid"} <= outcomes
    assert sum(1 for o in d.ORDERS if o.customer is None) == 1
    assert [o.days_ago for o in d.ORDERS] == sorted((o.days_ago for o in d.ORDERS), reverse=True)  # oldest first


def test_customers_use_the_reserved_example_domain_so_no_real_person_is_ever_emailed():
    assert all(c.email.endswith("@example.com") for c in d.CUSTOMERS) and d.APPROVER_EMAIL.endswith("@example.com")
    assert d.GUEST["email"].endswith("@example.com")
    assert len({c.email for c in d.CUSTOMERS}) == len(d.CUSTOMERS)


def test_the_printed_env_lines_are_what_the_application_reads():
    names = {line.split("=")[0] for line in d.ENV_LINES.splitlines()}
    assert names == {"PAYMENT_CURRENCIES_ENABLED", "FLAT_SHIPPING_CENTS", "FREE_SHIPPING_THRESHOLD_CENTS",
                     "FREE_SHIPPING_THRESHOLD_BASIS", "TAX_RATE_PERCENT"}
