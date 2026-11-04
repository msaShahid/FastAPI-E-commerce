# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""Which writes announce which storefront tags, and only AFTER they are committed."""

import pytest
from test_cart_variants import signup
from test_coupons import fill, place, shop
from test_order_payment_lifecycle import ADDR, category  # noqa: F401  (ADDR kept for parity with sibling suites)

from app.core import revalidate

API = "/api/v1"


@pytest.fixture
def announced(monkeypatch):
    sent = []
    monkeypatch.setattr(revalidate, "schedule_notify", lambda tags: sent.append(sorted(tags)))
    return sent


async def test_editing_a_product_announces_the_catalogue_and_that_product_after_the_commit(client, admin_headers, category, announced):
    product = await shop(client, admin_headers, category, price=1000)
    announced.clear()
    r = await client.patch(f"{API}/products/{product['id']}", json={"price_cents": 2000}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert announced == [["catalogue", f"product:{product['id']}"]]


async def test_a_request_that_only_reads_or_fails_announces_nothing(client, admin_headers, category, announced):
    product = await shop(client, admin_headers, category)
    announced.clear()
    assert (await client.get(f"{API}/products/{product['id']}")).status_code == 200
    assert (await client.patch(f"{API}/products/{product['id']}", json={"price_cents": -5}, headers=admin_headers)).status_code == 422
    assert (await client.patch(f"{API}/products/999999", json={"price_cents": 5}, headers=admin_headers)).status_code == 404
    assert announced == []


async def test_a_category_change_announces_the_catalogue_and_that_category(client, admin_headers, announced):
    r = await client.post(f"{API}/categories", json={"name": "Hats", "description": None}, headers=admin_headers)
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert ["catalogue", f"category:{cid}"] in announced


async def test_buying_something_does_not_flood_the_storefront(client, admin_headers, category, announced):
    product = await shop(client, admin_headers, category, price=1000, stock=10)
    headers, _ = await signup(client)
    await fill(client, headers, product["id"], qty=1)
    announced.clear()
    assert (await place(client, headers)).status_code in (200, 201)  # the stock decrement is a bulk write: not announced (availability states change rarely)
    assert not any(t.startswith("product:") for tags in announced for t in tags)
