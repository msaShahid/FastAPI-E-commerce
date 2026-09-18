"""
The test that actually proves checkout is concurrency-safe.

"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.modules.auth.models.user import User
from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.categories.models.category import Category
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.models.product import Product
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole


def _build_service(session) -> OrderService:
    """Wires a real OrderService against one specific session."""
    return OrderService(
        OrderRepository(session),
        CartRepository(session),
        ProductRepository(session),
    )


async def _seed_shared_data(db_session):
    """
    Creates one category, one product with stock=1, and two users --
    each with a cart containing that single unit. Committed (not just
    flushed) so the two independent sessions below can both see it.
    """
    category = Category(name="Concurrency Test", slug="concurrency-test")
    db_session.add(category)
    await db_session.flush()

    product = Product(
        name="Last One Left",
        slug="last-one-left",
        price_cents=5000,
        sku="LAST-001",
        stock=1,  # THE critical setup: exactly one unit available
        category_id=category.id,
        status=ProductStatus.ACTIVE,
    )
    db_session.add(product)
    await db_session.flush()

    users = []
    for i in range(2):
        user = User(
            id=uuid.uuid4(),
            username=f"racer{i}",
            email=f"racer{i}@example.com",
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
        users.append(user)

    await db_session.commit()
    return product, users


async def _attempt_checkout(session_factory, user_id, idempotency_key):
    """
    One complete checkout attempt in its OWN session/transaction --
    mirroring exactly what get_db does per-request in production:
    commit on success, rollback on failure.
    """
    async with session_factory() as session:
        service = _build_service(session)
        try:
            order = await service.checkout(
                user_id=user_id, idempotency_key=idempotency_key
            )
            await session.commit()
            return ("success", order.id)
        except Exception as exc:
            await session.rollback()
            # str(exc) included now -- "AttributeError" alone doesn't
            # say WHICH attribute was missing; this does.
            return ("failed", f"{type(exc).__name__}: {exc}")


async def test_concurrent_checkout_on_last_unit_only_one_succeeds(
    db_session, test_session_factory
):
    """
    THE test. Two users, one unit of stock, simultaneous checkout.

    Correct behavior: exactly one succeeds, one fails cleanly with
    InsufficientStockError, and final stock is 0 -- never -1, never
    two successful orders for one unit.

    Without SELECT FOR UPDATE, both transactions would read stock=1,
    both would pass the check, and both would decrement -- producing
    either stock=-1 (caught only by the CHECK constraint, as an ugly
    IntegrityError) or two orders for one physical item.
    """
    product, users = await _seed_shared_data(db_session)

    results = await asyncio.gather(
        _attempt_checkout(test_session_factory, users[0].id, "race-key-user-0"),
        _attempt_checkout(test_session_factory, users[1].id, "race-key-user-1"),
    )

    outcomes = [r[0] for r in results]
    assert outcomes.count("success") == 1, f"Expected exactly 1 success, got: {results}"
    assert outcomes.count("failed") == 1, f"Expected exactly 1 failure, got: {results}"

    # The failure must be a clean, expected domain error -- NOT an
    # IntegrityError from the CHECK constraint (which would mean the
    # lock didn't work and we only got saved by the last line of
    # defense) and NOT a deadlock.
    failure = next(r for r in results if r[0] == "failed")
    assert failure[1].startswith("InsufficientStockError"), (
        f"Expected a clean InsufficientStockError, got: {failure[1]} -- "
        "if this is IntegrityError, the row lock isn't working and the "
        "CHECK constraint is the only thing preventing negative stock."
    )

    # Verify final stock in a FRESH session (not db_session, which may
    # hold a stale cached copy of the product from _seed_shared_data).
    async with test_session_factory() as verify_session:
        result = await verify_session.execute(
            select(Product).where(Product.id == product.id)
        )
        final_product = result.scalar_one()
        assert final_product.stock == 0, f"Expected stock=0, got {final_product.stock}"


async def test_sequential_checkouts_on_last_unit_second_fails_cleanly(
    db_session, test_session_factory
):
    """
    The non-concurrent control case: the same scenario run one after
    the other. This SHOULD pass even without row locking -- it exists
    to prove the test above is actually testing concurrency, not just
    basic stock validation. If this one failed, the problem would be
    ordinary logic, not a race.
    """
    product, users = await _seed_shared_data(db_session)

    first = await _attempt_checkout(test_session_factory, users[0].id, "seq-key-0")
    second = await _attempt_checkout(test_session_factory, users[1].id, "seq-key-1")

    assert first[0] == "success"
    assert second[0] == "failed"
    assert second[1].startswith("InsufficientStockError")
