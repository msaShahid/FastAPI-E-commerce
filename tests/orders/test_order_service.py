from uuid import uuid4

import pytest

from app.modules.cart.exceptions.cart_exceptions import InsufficientStockError
from app.modules.orders.exceptions.order_exceptions import (
    EmptyCartError,
    InvalidStatusTransitionError,
    OrderAccessForbiddenError,
    OrderNotFoundError,
)
from app.modules.orders.services.order_service import OrderService
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus


@pytest.fixture
def order_service(
    fake_order_repository,
    fake_cart_repository_for_orders,
    fake_product_repository_for_orders,
) -> OrderService:
    return OrderService(
        fake_order_repository,
        fake_cart_repository_for_orders,
        fake_product_repository_for_orders,
    )


async def _make_product(repo, **overrides):
    defaults = dict(
        name="Wireless Mouse",
        description=None,
        price_cents=1999,
        sku="MOUSE-001",
        stock=10,
        category_id=1,
        status=ProductStatus.ACTIVE,
    )
    defaults.update(overrides)
    return await repo.create(**defaults)


async def _seed_cart(cart_repo, user_id, product, quantity=2):
    cart = await cart_repo.get_or_create_cart(user_id)
    cart_repo.add_item_directly(cart, product.id, quantity, product.price_cents)
    return cart


# --- checkout: happy path ---


async def test_checkout_creates_order_with_snapshots(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    user_id = uuid4()
    product = await _make_product(
        fake_product_repository_for_orders, price_cents=1999, stock=10
    )
    await _seed_cart(fake_cart_repository_for_orders, user_id, product, quantity=2)

    order = await order_service.checkout(user_id=user_id, idempotency_key="key-1")

    assert order.status == OrderStatus.PENDING
    assert len(order.items) == 1
    assert order.items[0].product_name_snapshot == "Wireless Mouse"
    assert order.items[0].price_cents_snapshot == 1999
    assert order.subtotal_cents == 3998  # 1999 * 2
    assert order.total_cents == 3998


async def test_checkout_decrements_product_stock(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product, quantity=3)

    await order_service.checkout(user_id=user_id, idempotency_key="key-2")

    assert product.stock == 7


async def test_checkout_clears_the_cart(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    cart = await _seed_cart(fake_cart_repository_for_orders, user_id, product)

    await order_service.checkout(user_id=user_id, idempotency_key="key-3")

    assert cart.items == []


async def test_checkout_uses_row_locking_not_plain_read(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """
    Proves checkout calls get_by_id_for_update (the SELECT ... FOR
    UPDATE path), NOT the unlocked get_by_id. This is the single most
    important behavioral guarantee in this stage -- a future refactor
    that accidentally swapped these would silently reintroduce the
    race condition with no other visible symptom.
    """
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product)

    await order_service.checkout(user_id=user_id, idempotency_key="key-4")

    assert fake_product_repository_for_orders.lock_calls == [product.id]


async def test_checkout_locks_products_in_ascending_id_order(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """
    Deadlock avoidance: products must always be locked in a consistent
    order across every concurrent checkout. Seeds the cart with items
    added in DESCENDING id order and asserts they're still locked
    ascending.
    """
    user_id = uuid4()
    p1 = await _make_product(fake_product_repository_for_orders, sku="A-1", stock=10)
    p2 = await _make_product(fake_product_repository_for_orders, sku="B-2", stock=10)

    cart = await fake_cart_repository_for_orders.get_or_create_cart(user_id)
    fake_cart_repository_for_orders.add_item_directly(cart, p2.id, 1, p2.price_cents)
    fake_cart_repository_for_orders.add_item_directly(cart, p1.id, 1, p1.price_cents)

    await order_service.checkout(user_id=user_id, idempotency_key="key-5")

    assert fake_product_repository_for_orders.lock_calls == [p1.id, p2.id]


# --- checkout: failure paths ---


async def test_checkout_empty_cart_raises(order_service):
    with pytest.raises(EmptyCartError):
        await order_service.checkout(user_id=uuid4(), idempotency_key="key-6")


async def test_checkout_insufficient_stock_raises(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=1)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product, quantity=5)

    with pytest.raises(InsufficientStockError):
        await order_service.checkout(user_id=user_id, idempotency_key="key-7")


async def test_checkout_is_idempotent(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """
    THE idempotency guarantee: same key twice returns the SAME order,
    and critically, does NOT decrement stock a second time.
    """
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product, quantity=2)

    first = await order_service.checkout(user_id=user_id, idempotency_key="same-key")
    stock_after_first = product.stock

    second = await order_service.checkout(user_id=user_id, idempotency_key="same-key")

    assert first.id == second.id
    assert product.stock == stock_after_first  # no double decrement


# --- authorization ---


async def test_user_cannot_view_another_users_order(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    owner_id = uuid4()
    other_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, owner_id, product)
    order = await order_service.checkout(user_id=owner_id, idempotency_key="key-8")

    with pytest.raises(OrderAccessForbiddenError):
        await order_service.get_order(
            order_id=order.id, user_id=other_id, is_admin=False
        )


async def test_admin_can_view_any_order(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    owner_id = uuid4()
    admin_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, owner_id, product)
    order = await order_service.checkout(user_id=owner_id, idempotency_key="key-9")

    result = await order_service.get_order(
        order_id=order.id, user_id=admin_id, is_admin=True
    )

    assert result.id == order.id


async def test_get_nonexistent_order_raises(order_service):
    with pytest.raises(OrderNotFoundError):
        await order_service.get_order(order_id=uuid4(), user_id=uuid4(), is_admin=True)


# --- status transitions ---


async def test_valid_status_transition_succeeds(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product)
    order = await order_service.checkout(user_id=user_id, idempotency_key="key-10")

    updated = await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.PAID, changed_by_user_id=uuid4()
    )

    assert updated.status == OrderStatus.PAID


async def test_invalid_status_transition_rejected(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """PENDING -> FULFILLED skips PAID entirely and must be rejected."""
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product)
    order = await order_service.checkout(user_id=user_id, idempotency_key="key-11")

    with pytest.raises(InvalidStatusTransitionError):
        await order_service.update_status(
            order_id=order.id,
            new_status=OrderStatus.FULFILLED,
            changed_by_user_id=uuid4(),
        )


async def test_cancelled_order_cannot_be_reactivated(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """CANCELLED is terminal -- nothing can follow it."""
    user_id = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product)
    order = await order_service.checkout(user_id=user_id, idempotency_key="key-12")

    await order_service.update_status(
        order_id=order.id, new_status=OrderStatus.CANCELLED, changed_by_user_id=uuid4()
    )

    with pytest.raises(InvalidStatusTransitionError):
        await order_service.update_status(
            order_id=order.id, new_status=OrderStatus.PAID, changed_by_user_id=uuid4()
        )
