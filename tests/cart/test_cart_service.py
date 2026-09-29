from uuid import uuid4

import pytest

from app.modules.cart.exceptions.cart_exceptions import (
    CartItemNotFoundError,
    InsufficientStockError,
)
from app.modules.cart.services.cart_service import CartService
from app.shared.enums.product_status import ProductStatus


@pytest.fixture
def cart_service(
    fake_cart_repository,
    fake_product_repository,
) -> CartService:
    return CartService(
        fake_cart_repository,
        fake_product_repository,
    )


async def _make_product(fake_product_repository, **overrides):
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

    return await fake_product_repository.create(**defaults)


async def test_add_new_item_stores_current_price_snapshot(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()

    product = await _make_product(
        fake_product_repository,
        price_cents=1999,
    )

    cart = await cart_service.add_item(
        user_id=user_id,
        product_id=product.id,
        quantity=2,
    )

    assert len(cart.items) == 1

    item = cart.items[0]

    assert item.product_id == product.id
    assert item.quantity == 2
    assert item.price_cents_snapshot == 1999


async def test_add_existing_product_increments_quantity(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()

    product = await _make_product(
        fake_product_repository,
        price_cents=1999,
        stock=10,
    )

    await cart_service.add_item(
        user_id=user_id,
        product_id=product.id,
        quantity=2,
    )

    cart = await cart_service.add_item(
        user_id=user_id,
        product_id=product.id,
        quantity=3,
    )

    assert len(cart.items) == 1
    assert cart.items[0].quantity == 5


async def test_add_more_than_available_stock_raises_error(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()

    product = await _make_product(
        fake_product_repository,
        stock=3,
    )

    with pytest.raises(InsufficientStockError):
        await cart_service.add_item(
            user_id=user_id,
            product_id=product.id,
            quantity=4,
        )


async def test_user_cannot_update_another_users_cart_item(
    cart_service,
    fake_product_repository,
):
    owner_id = uuid4()
    other_user_id = uuid4()

    product = await _make_product(
        fake_product_repository,
        stock=10,
    )

    cart = await cart_service.add_item(
        user_id=owner_id,
        product_id=product.id,
        quantity=2,
    )

    item_id = cart.items[0].id

    with pytest.raises(CartItemNotFoundError):
        await cart_service.update_item_quantity(
            user_id=other_user_id,
            item_id=item_id,
            quantity=5,
        )


async def test_user_cannot_remove_another_users_cart_item(
    cart_service,
    fake_product_repository,
):
    owner_id = uuid4()
    other_user_id = uuid4()

    product = await _make_product(
        fake_product_repository,
        stock=10,
    )

    cart = await cart_service.add_item(
        user_id=owner_id,
        product_id=product.id,
        quantity=2,
    )

    item_id = cart.items[0].id

    with pytest.raises(CartItemNotFoundError):
        await cart_service.remove_item(
            user_id=other_user_id,
            item_id=item_id,
        )


# --- guest cart ---


async def test_guest_can_add_item_without_a_user_id(
    cart_service,
    fake_product_repository,
):
    guest_token = "guest-token-1"
    product = await _make_product(fake_product_repository, stock=10)

    cart = await cart_service.add_item(
        guest_token=guest_token, product_id=product.id, quantity=2
    )

    assert cart.user_id is None
    assert cart.guest_token == guest_token
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 2


async def test_guest_cart_is_isolated_by_token(
    cart_service,
    fake_product_repository,
):
    product = await _make_product(fake_product_repository, stock=10)

    await cart_service.add_item(
        guest_token="guest-a", product_id=product.id, quantity=1
    )
    cart_b = await cart_service.get_cart(guest_token="guest-b")

    assert cart_b.items == []


async def test_guest_cannot_touch_another_guests_cart_item(
    cart_service,
    fake_product_repository,
):
    product = await _make_product(fake_product_repository, stock=10)

    cart = await cart_service.add_item(
        guest_token="guest-a", product_id=product.id, quantity=1
    )
    item_id = cart.items[0].id

    with pytest.raises(CartItemNotFoundError):
        await cart_service.remove_item(guest_token="guest-b", item_id=item_id)


async def test_merge_guest_cart_with_no_existing_guest_cart_is_a_noop(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository, stock=10)
    await cart_service.add_item(user_id=user_id, product_id=product.id, quantity=1)

    await cart_service.merge_guest_cart_into_user(
        user_id=user_id, guest_token="never-used"
    )

    cart = await cart_service.get_cart(user_id)
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 1


async def test_merge_guest_cart_into_user_with_no_cart_yet_reassigns_it(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository, stock=10)
    await cart_service.add_item(
        guest_token="guest-a", product_id=product.id, quantity=2
    )

    await cart_service.merge_guest_cart_into_user(user_id=user_id, guest_token="guest-a")

    cart = await cart_service.get_cart(user_id)
    assert cart.guest_token is None
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 2

    # The guest cart itself is gone -- reassigned, not duplicated.
    assert await cart_service.repository.get_cart_by_guest_token("guest-a") is None


async def test_merge_guest_cart_sums_quantity_for_a_product_in_both_carts(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()
    product = await _make_product(fake_product_repository, stock=20)

    await cart_service.add_item(user_id=user_id, product_id=product.id, quantity=3)
    await cart_service.add_item(
        guest_token="guest-a", product_id=product.id, quantity=4
    )

    await cart_service.merge_guest_cart_into_user(user_id=user_id, guest_token="guest-a")

    cart = await cart_service.get_cart(user_id)
    assert len(cart.items) == 1
    assert cart.items[0].quantity == 7


async def test_merge_guest_cart_adds_disjoint_products_alongside_existing_ones(
    cart_service,
    fake_product_repository,
):
    user_id = uuid4()
    product_a = await _make_product(fake_product_repository, sku="A-001", stock=10)
    product_b = await _make_product(fake_product_repository, sku="B-001", stock=10)

    await cart_service.add_item(user_id=user_id, product_id=product_a.id, quantity=1)
    await cart_service.add_item(
        guest_token="guest-a", product_id=product_b.id, quantity=5
    )

    await cart_service.merge_guest_cart_into_user(user_id=user_id, guest_token="guest-a")

    cart = await cart_service.get_cart(user_id)
    quantities = {item.product_id: item.quantity for item in cart.items}
    assert quantities == {product_a.id: 1, product_b.id: 5}