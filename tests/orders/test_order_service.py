from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.modules.cart.exceptions.cart_exceptions import InsufficientStockError
from app.modules.orders.exceptions.order_exceptions import (
    EmptyCartError,
    InvalidStatusTransitionError,
    OrderAccessForbiddenError,
    OrderNotFoundError,
)
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.orders.services.order_service import OrderService
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus


def _make_stale_order(order_repo, product, *, quantity=2, age_minutes=60) -> Order:
    """
    Builds a PENDING order directly (bypassing checkout) with an
    explicit created_at, so tests can control exactly how "stale" it
    is. Mirrors FakeOrderRepository.create_order's item-building
    pattern.
    """
    order_id = uuid4()
    created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=age_minutes)
    order = Order(
        id=order_id,
        user_id=uuid4(),
        idempotency_key=f"stale-{order_id}",
        status=OrderStatus.PENDING,
        subtotal_cents=product.price_cents * quantity,
        shipping_cents=0,
        tax_cents=0,
        total_cents=product.price_cents * quantity,
        created_at=created_at,
    )
    order.items = [
        OrderItem(
            id=uuid4().int % 1_000_000,
            order_id=order_id,
            product_id=product.id,
            product_name_snapshot=product.name,
            price_cents_snapshot=product.price_cents,
            quantity=quantity,
        )
    ]
    order_repo.orders[order_id] = order
    return order


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


async def test_checkout_idempotency_key_is_scoped_per_user(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """
    Two different users sending the identical Idempotency-Key value must
    each get their OWN order -- a shared/colliding key value must never
    return one user's order to a different user. (Previously this was a
    global unique key: a collision returned the wrong user's order.)
    """
    user_a = uuid4()
    user_b = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_a, product, quantity=1)
    await _seed_cart(fake_cart_repository_for_orders, user_b, product, quantity=1)

    order_a = await order_service.checkout(user_id=user_a, idempotency_key="shared-key")
    order_b = await order_service.checkout(user_id=user_b, idempotency_key="shared-key")

    assert order_a.id != order_b.id
    assert order_a.user_id == user_a
    assert order_b.user_id == user_b
    assert product.stock == 8  # both checkouts actually went through


async def test_checkout_retry_still_returns_same_order_after_per_user_scoping(
    order_service, fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """Same user, same key, called via two different users' unrelated
    checkouts in between -- must not affect this user's own retry."""
    user_id = uuid4()
    other_user = uuid4()
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    await _seed_cart(fake_cart_repository_for_orders, user_id, product, quantity=1)
    await _seed_cart(fake_cart_repository_for_orders, other_user, product, quantity=1)

    first = await order_service.checkout(user_id=user_id, idempotency_key="dup-key")
    await order_service.checkout(user_id=other_user, idempotency_key="dup-key")
    second = await order_service.checkout(user_id=user_id, idempotency_key="dup-key")

    assert first.id == second.id


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


# --- cancel_stale_pending_orders (stock-release reaper) ---


async def test_cancel_stale_pending_orders_restocks_and_cancels(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    product = await _make_product(fake_product_repository_for_orders, stock=5)
    order = _make_stale_order(
        fake_order_repository, product, quantity=2, age_minutes=60
    )
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)

    cancelled_ids = await order_service.cancel_stale_pending_orders(older_than=cutoff)

    assert cancelled_ids == [order.id]
    assert order.status == OrderStatus.CANCELLED
    assert product.stock == 7  # 5 + the 2 units the abandoned order held


async def test_cancel_stale_pending_orders_ignores_recent_orders(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    product = await _make_product(fake_product_repository_for_orders, stock=5)
    order = _make_stale_order(fake_order_repository, product, quantity=2, age_minutes=5)
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)

    cancelled_ids = await order_service.cancel_stale_pending_orders(older_than=cutoff)

    assert cancelled_ids == []
    assert order.status == OrderStatus.PENDING
    assert product.stock == 5  # untouched


async def test_cancel_stale_pending_orders_ignores_non_pending_orders(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    product = await _make_product(fake_product_repository_for_orders, stock=5)
    order = _make_stale_order(
        fake_order_repository, product, quantity=2, age_minutes=60
    )
    order.status = OrderStatus.CANCELLED  # e.g. an admin already cancelled it
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)

    cancelled_ids = await order_service.cancel_stale_pending_orders(older_than=cutoff)

    assert cancelled_ids == []
    assert product.stock == 5  # not restocked a second time


async def test_cancel_stale_pending_orders_locks_products_in_ascending_id_order(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    """Same deadlock-avoidance rule as checkout applies to restocking."""
    p1 = await _make_product(fake_product_repository_for_orders, sku="A-1", stock=5)
    p2 = await _make_product(fake_product_repository_for_orders, sku="B-2", stock=5)
    order_id = uuid4()
    created_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=60)
    order = Order(
        id=order_id,
        user_id=uuid4(),
        idempotency_key=f"stale-{order_id}",
        status=OrderStatus.PENDING,
        subtotal_cents=0,
        shipping_cents=0,
        tax_cents=0,
        total_cents=0,
        created_at=created_at,
    )
    # Items deliberately built in DESCENDING product-id order.
    order.items = [
        OrderItem(
            id=1,
            order_id=order_id,
            product_id=p2.id,
            product_name_snapshot=p2.name,
            price_cents_snapshot=p2.price_cents,
            quantity=1,
        ),
        OrderItem(
            id=2,
            order_id=order_id,
            product_id=p1.id,
            product_name_snapshot=p1.name,
            price_cents_snapshot=p1.price_cents,
            quantity=1,
        ),
    ]
    fake_order_repository.orders[order_id] = order
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)

    await order_service.cancel_stale_pending_orders(older_than=cutoff)

    assert fake_product_repository_for_orders.lock_calls == [p1.id, p2.id]


async def test_cancel_stale_pending_orders_skips_order_that_became_paid_before_lock(
    fake_cart_repository_for_orders, fake_product_repository_for_orders
):
    """
    The whole point of re-checking status AFTER acquiring the lock: a
    customer completing payment in the gap between listing candidates
    and locking this specific order must not have it cancelled or
    restocked out from under them.
    """
    from tests.orders.conftest import FakeOrderRepository

    class RacyOrderRepository(FakeOrderRepository):
        async def list_stale_pending_order_ids(self, *, older_than):
            ids = await super().list_stale_pending_order_ids(older_than=older_than)
            for order_id in ids:
                self.orders[order_id].status = OrderStatus.PAID
            return ids

    order_repo = RacyOrderRepository()
    service = OrderService(
        order_repo, fake_cart_repository_for_orders, fake_product_repository_for_orders
    )
    product = await _make_product(fake_product_repository_for_orders, stock=5)
    order = _make_stale_order(order_repo, product, quantity=2, age_minutes=60)
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)

    cancelled_ids = await service.cancel_stale_pending_orders(older_than=cutoff)

    assert cancelled_ids == []
    assert order.status == OrderStatus.PAID  # untouched by the reaper
    assert product.stock == 5  # NOT restocked


# --- list_all_orders (admin) ---


async def test_list_all_orders_returns_every_users_orders(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    order_a = _make_stale_order(fake_order_repository, product, age_minutes=5)
    order_b = _make_stale_order(fake_order_repository, product, age_minutes=1)
    assert order_a.user_id != order_b.user_id  # different users, both should show up

    orders, total = await order_service.list_all_orders(offset=0, limit=20)

    assert total == 2
    assert {o.id for o in orders} == {order_a.id, order_b.id}


async def test_list_all_orders_filters_by_status(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    pending_order = _make_stale_order(fake_order_repository, product, age_minutes=5)
    paid_order = _make_stale_order(fake_order_repository, product, age_minutes=1)
    paid_order.status = OrderStatus.PAID

    orders, total = await order_service.list_all_orders(
        offset=0, limit=20, status=OrderStatus.PAID
    )

    assert total == 1
    assert orders[0].id == paid_order.id
    assert pending_order.id not in {o.id for o in orders}


async def test_list_all_orders_paginates(
    order_service, fake_order_repository, fake_product_repository_for_orders
):
    product = await _make_product(fake_product_repository_for_orders, stock=10)
    for i in range(5):
        _make_stale_order(fake_order_repository, product, age_minutes=i)

    page_1, total = await order_service.list_all_orders(offset=0, limit=2)
    page_2, _ = await order_service.list_all_orders(offset=2, limit=2)

    assert total == 5
    assert len(page_1) == 2
    assert len(page_2) == 2
    assert {o.id for o in page_1}.isdisjoint({o.id for o in page_2})
