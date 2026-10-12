"""
Many buyers, few units: checkout must never sell stock that is gone.

test_checkout_concurrency.py proves the 2-buyer / 1-unit case, but with only
two contenders the outcome depends on timing (one often finishes before the
other has even read its cart), so a stale-read bug can slip through. This
test uses many more buyers than units so the race is exercised reliably.

Regression for: checkout loaded each cart item's Product BEFORE taking the
row lock, then trusted that stale `stock` after the lock was granted
(`SELECT ... FOR UPDATE` without populate_existing). Under load that sold
32 units of a 10-unit product.
"""

import asyncio
import uuid

from sqlalchemy import func, select

from app.modules.auth.models.user import User
from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.categories.models.category import Category
from app.modules.orders.models.order_item import OrderItem
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.models.product import Product
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole

BUYERS = 12
STOCK = 3


async def _seed(db_session):
    category = Category(name="Oversell Test", slug="oversell-test")
    db_session.add(category)
    await db_session.flush()

    product = Product(
        name="Scarce Item",
        slug="scarce-item",
        price_cents=2500,
        sku="SCARCE-001",
        stock=STOCK,
        category_id=category.id,
        status=ProductStatus.ACTIVE,
    )
    db_session.add(product)
    await db_session.flush()

    user_ids = []
    for i in range(BUYERS):
        user = User(
            id=uuid.uuid4(),
            username=f"buyer{i}",
            email=f"buyer{i}@example.com",
            password_hash="irrelevant-for-this-test",
            role=UserRole.USER,
            is_active=True,
        )
        db_session.add(user)
        await db_session.flush()

        cart = Cart(user_id=user.id)
        db_session.add(cart)
        await db_session.flush()
        db_session.add(
            CartItem(
                cart_id=cart.id,
                product_id=product.id,
                quantity=1,
                price_cents_snapshot=product.price_cents,
            )
        )
        user_ids.append(user.id)

    await db_session.commit()
    return product.id, user_ids


async def _checkout(session_factory, user_id):
    # One session/transaction per attempt, like get_db does per request.
    async with session_factory() as session:
        service = OrderService(
            OrderRepository(session), CartRepository(session), ProductRepository(session)
        )
        try:
            await service.checkout(user_id=user_id, idempotency_key=f"key-{user_id}")
            await session.commit()
            return "success"
        except Exception as exc:
            await session.rollback()
            return type(exc).__name__


async def test_many_buyers_never_oversell_a_scarce_product(db_session, test_session_factory):
    product_id, user_ids = await _seed(db_session)

    results = await asyncio.gather(*(_checkout(test_session_factory, uid) for uid in user_ids))

    assert results.count("success") == STOCK, f"expected {STOCK} successes, got: {results}"
    losers = [r for r in results if r != "success"]
    assert set(losers) == {"InsufficientStockError"}, f"unexpected failures: {losers}"

    async with test_session_factory() as session:
        stock = (
            await session.execute(select(Product.stock).where(Product.id == product_id))
        ).scalar_one()
        units_sold = (
            await session.execute(
                select(func.coalesce(func.sum(OrderItem.quantity), 0)).where(
                    OrderItem.product_id == product_id
                )
            )
        ).scalar_one()

    assert units_sold == STOCK, f"{units_sold} units sold from a stock of {STOCK}"
    assert stock == 0
