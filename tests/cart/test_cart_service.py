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
