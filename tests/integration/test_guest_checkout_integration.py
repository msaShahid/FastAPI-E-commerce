"""Guest shopper over HTTP: add to cart -> checkout -> idempotent replay."""

import uuid

import pytest

SHIPPING = {
    "recipient_name": "Guest Buyer",
    "line1": "1 Test Street",
    "city": "Testville",
    "state": "CA",
    "postal_code": "94000",
    "country": "US",
}


@pytest.fixture
async def product(client, admin_headers):
    category = await client.post(
        "/api/v1/categories", json={"name": "Guest Cat", "description": None}, headers=admin_headers
    )
    assert category.status_code == 201, category.text
    response = await client.post(
        "/api/v1/products",
        json={
            "name": "Guest Item",
            "description": None,
            "price_cents": 1500,
            "sku": "GUEST-001",
            "stock": 2,
            "category_id": category.json()["id"],
            "status": "active",
        },
        headers=admin_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_first_add_to_cart_as_guest_succeeds(client, product):
    # Regression: the first add on a brand-new cart used to return 500
    # (MissingGreenlet from `cart.items = []` after the flush).
    response = await client.post(
        "/api/v1/cart/items", json={"product_id": product["id"], "quantity": 1}
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["item_count"] == 1
    assert body["subtotal_cents"] == 1500
    assert "guest_cart_token" in response.cookies


async def test_guest_checkout_creates_order_takes_stock_and_replays_idempotently(client, product):
    await client.post("/api/v1/cart/items", json={"product_id": product["id"], "quantity": 1})
    key = str(uuid.uuid4())
    body = {"guest_email": "guest@example.com", "shipping_address": SHIPPING}

    first = await client.post("/api/v1/orders/checkout", json=body, headers={"Idempotency-Key": key})
    assert first.status_code == 201, first.text

    replay = await client.post("/api/v1/orders/checkout", json=body, headers={"Idempotency-Key": key})
    assert replay.status_code in (200, 201), replay.text
    assert replay.json()["id"] == first.json()["id"]

    stock = (await client.get(f"/api/v1/products/{product['id']}")).json()["stock"]
    assert stock == 1  # one unit taken, the replay took nothing
