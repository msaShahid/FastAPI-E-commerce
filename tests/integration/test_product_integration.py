"""
Integration tests for products -- focused on the database-level
guarantees that only a real Postgres connection can prove: the
price/stock CHECK constraints, and the category_id foreign key's
ondelete="RESTRICT" behavior from Stage 8.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.categories.models.category import Category
from app.modules.products.models.product import Product
from app.shared.enums.product_status import ProductStatus


async def _make_category(db_session, name="Electronics", slug="electronics") -> Category:
    category = Category(name=name, slug=slug)
    db_session.add(category)
    await db_session.flush()
    return category


async def test_price_check_constraint_enforced_at_db_level(db_session):
    """
    Bypasses Pydantic's gt=0 validation entirely (that's an API-layer
    guard, not a database one) -- proves the CHECK constraint from
    Stage 8 is a real, independent line of defense, not just
    documentation on the model.
    """
    category = await _make_category(db_session)

    db_session.add(
        Product(
            name="Broken Product",
            slug="broken-product",
            price_cents=0,  # violates ck_products_price_positive
            sku="BROKEN-001",
            stock=10,
            category_id=category.id,
            status=ProductStatus.DRAFT,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_negative_stock_check_constraint_enforced_at_db_level(db_session):
    category = await _make_category(db_session, name="Books", slug="books")

    db_session.add(
        Product(
            name="Negative Stock Product",
            slug="negative-stock-product",
            price_cents=999,
            sku="NEG-001",
            stock=-1,  # violates ck_products_stock_non_negative
            category_id=category.id,
            status=ProductStatus.DRAFT,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_category_with_products_cannot_be_hard_deleted(db_session):
    """
    The test that actually proves ondelete="RESTRICT" works, at the
    database level, independent of the application never issuing a
    hard DELETE through its own API (categories only ever get
    deactivated via the real endpoints -- this test simulates the
    "what if someone deletes the row directly" scenario the RESTRICT
    behavior exists to guard against).
    """
    category = await _make_category(db_session, name="Toys", slug="toys")
    db_session.add(
        Product(
            name="Toy Car",
            slug="toy-car",
            price_cents=1999,
            sku="TOY-001",
            stock=5,
            category_id=category.id,
            status=ProductStatus.ACTIVE,
        )
    )
    await db_session.flush()

    await db_session.delete(category)
    with pytest.raises(IntegrityError):
        await db_session.flush()


# async def test_create_product_as_admin_succeeds_end_to_end(client, admin_headers, db_session):
#     category = await _make_category(db_session, name="Sports", slug="sports")

#     response = await client.post(
#         "/api/v1/products",
#         json={
#             "name": "Tennis Racket",
#             "description": None,
#             "price_cents": 8999,
#             "sku": "SPORT-005",
#             "stock": 15,
#             "category_id": category.id,
#             "status": "active",
#         },
#         headers=admin_headers,
#     )
#     assert response.status_code == 201
#     body = response.json()
#     assert body["category"]["id"] == category.id
#     assert body["price_cents"] == 8999