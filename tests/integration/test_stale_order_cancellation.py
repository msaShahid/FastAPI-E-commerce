"""
Real-Postgres check for OrderService.cancel_stale_pending_orders --
the fake-repository unit tests in tests/orders/ cover the branching
logic, this confirms the actual SQL (the eager-loaded FOR UPDATE query,
the created_at comparison, the restock) works against a real database.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.modules.auth.models.user import User
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.categories.models.category import Category
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.models.product import Product
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole


async def _seed_stale_order(db_session, *, age_minutes: int, quantity: int = 2):
    unique = uuid.uuid4().hex[:8]
    category = Category(name=f"Stale Order Test {unique}", slug=f"stale-order-test-{unique}")
    db_session.add(category)
    await db_session.flush()

    product = Product(
        name="Abandoned Widget",
        slug=f"abandoned-widget-{unique}",
        price_cents=1000,
        sku=f"STALE-{unique}",
        stock=5,
        category_id=category.id,
        status=ProductStatus.ACTIVE,
    )
    db_session.add(product)
    await db_session.flush()

    user = User(
        id=uuid.uuid4(),
        username=f"abandoner_{unique}",
        email=f"abandoner_{unique}@example.com",
        password_hash="irrelevant-for-this-test",
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        idempotency_key=f"stale-{uuid.uuid4()}",
        status=OrderStatus.PENDING,
        subtotal_cents=1000 * quantity,
        shipping_cents=0,
        tax_cents=0,
        total_cents=1000 * quantity,
    )
    db_session.add(order)
    await db_session.flush()

    db_session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            product_name_snapshot=product.name,
            price_cents_snapshot=product.price_cents,
            quantity=quantity,
        )
    )
    await db_session.flush()
    await db_session.commit()

    # created_at has server_default=now(), so it has to be backdated with
    # a raw UPDATE after the insert rather than set on the object.
    backdated_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=age_minutes)
    await db_session.execute(
        update(Order).where(Order.id == order.id).values(created_at=backdated_at)
    )
    await db_session.commit()

    return order.id, product.id


async def test_cancel_stale_pending_orders_against_real_db(db_session, test_session_factory):
    stale_order_id, product_id = await _seed_stale_order(db_session, age_minutes=60)
    fresh_order_id, _ = await _seed_stale_order(db_session, age_minutes=5)

    async with test_session_factory() as session:
        service = OrderService(
            OrderRepository(session),
            CartRepository(session),
            ProductRepository(session),
        )
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
        cancelled_ids = await service.cancel_stale_pending_orders(older_than=cutoff)
        await session.commit()

    assert stale_order_id in cancelled_ids
    assert fresh_order_id not in cancelled_ids

    async with test_session_factory() as verify_session:
        order_result = await verify_session.execute(
            select(Order).where(Order.id == stale_order_id)
        )
        assert order_result.scalar_one().status == OrderStatus.CANCELLED

        fresh_result = await verify_session.execute(
            select(Order).where(Order.id == fresh_order_id)
        )
        assert fresh_result.scalar_one().status == OrderStatus.PENDING

        product_result = await verify_session.execute(
            select(Product).where(Product.id == product_id)
        )
        # stock=5 seeded, minus nothing was ever decremented in this test
        # (orders were inserted directly, not via checkout) plus the
        # reaper's restock of 2 -- proves increment_stock actually ran.
        assert product_result.scalar_one().stock == 7
