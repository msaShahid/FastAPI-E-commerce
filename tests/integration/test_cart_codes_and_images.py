# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""Cart errors carry stable codes (a client never has to read the wording), and a cart line shows the product's image."""

from sqlalchemy import text
from test_cart_variants import add, cart, signup
from test_order_payment_lifecycle import category  # noqa: F401
from test_variant_pricing import simple_product

from app.core.config import get_settings

API = "/api/v1"


async def test_not_enough_stock_has_a_code_and_names_the_number_only_when_stock_is_low(client, db_session, category):
    low = await simple_product(db_session, category, stock=3)
    plenty = await simple_product(db_session, category, stock=500)
    headers, _ = await signup(client)

    r = await add(client, headers, product_id=low, quantity=5)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "insufficient_stock" and body["available"] == 3

    r = await add(client, headers, product_id=plenty, quantity=1000)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "insufficient_stock"
    assert "available" not in body and "500" not in r.text  # exact stock stays private above the display threshold


async def test_a_product_that_is_not_for_sale_has_its_own_code(client, db_session, category):
    pid = await simple_product(db_session, category, status="draft")
    headers, _ = await signup(client)
    r = await add(client, headers, product_id=pid, quantity=1)
    assert r.status_code == 422 and r.json()["code"] == "product_unavailable"


async def test_a_full_cart_has_a_code_and_the_limit(client, db_session, category, monkeypatch):
    monkeypatch.setattr("app.modules.cart.services.cart_service.MAX_CART_LINES", 1)
    a, b = await simple_product(db_session, category), await simple_product(db_session, category)
    headers, _ = await signup(client)
    assert (await add(client, headers, product_id=a, quantity=1)).status_code == 201
    r = await add(client, headers, product_id=b, quantity=1)
    assert r.status_code == 409
    assert r.json()["code"] == "cart_full" and r.json()["limit"] == 1


async def test_registering_with_a_taken_email_or_username_says_which(client):
    first = {"username": "taken_user", "email": "taken@example.com", "password": "CorrectHorse1!"}
    assert (await client.post(f"{API}/auth/register", json=first)).status_code == 201
    same_email = await client.post(f"{API}/auth/register", json={**first, "username": "someone_else"})
    assert same_email.status_code == 409 and same_email.json()["code"] == "email_already_registered"
    same_name = await client.post(f"{API}/auth/register", json={**first, "email": "other@example.com"})
    assert same_name.status_code == 409 and same_name.json()["code"] == "username_taken"


async def test_a_cart_line_carries_the_products_image_url(client, db_session, category):
    pid = await simple_product(db_session, category)
    plain = await simple_product(db_session, category)
    await db_session.execute(text("UPDATE products SET image_path = :p WHERE id = :id"), {"p": "products/mug.png", "id": pid})
    await db_session.commit()
    headers, _ = await signup(client)
    await add(client, headers, product_id=pid, quantity=1)
    await add(client, headers, product_id=plain, quantity=1)

    lines = {line["product"]["id"]: line["product"] for line in (await cart(client, headers))["items"]}
    catalogue = (await client.get(f"{API}/products/{pid}")).json()
    assert lines[pid]["image_url"] and lines[pid]["image_url"] == catalogue["image_url"]  # the same URL the catalogue shows
    assert lines[plain]["image_url"] is None


async def test_the_error_extras_do_not_leak_into_other_errors(client, db_session, category):
    headers, _ = await signup(client)
    r = await client.patch(f"{API}/cart/items/999999", json={"quantity": 2}, headers=headers)  # a missing resource: no code, no extras
    assert r.status_code == 404 and "code" not in r.json() and set(r.json()) <= {"detail", "request_id"}
    assert get_settings().low_stock_display_threshold >= 0
