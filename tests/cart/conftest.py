from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem
from app.modules.products.models.product import Product


class FakeCartRepository:
    def __init__(self) -> None:
        self.carts: dict[int, Cart] = {}
        self.items: dict[int, CartItem] = {}
        self._next_cart_id = 1
        self._next_item_id = 1

    async def get_or_create_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:
        if user_id is None and guest_token is None:
            raise ValueError("get_or_create_cart requires user_id or guest_token")

        cart = next(
            (
                c
                for c in self.carts.values()
                if (user_id is not None and c.user_id == user_id)
                or (guest_token is not None and c.guest_token == guest_token)
            ),
            None,
        )
        if cart is not None:
            return cart

        cart = Cart(id=self._next_cart_id, user_id=user_id, guest_token=guest_token)
        cart.items = []
        self.carts[self._next_cart_id] = cart
        self._next_cart_id += 1
        return cart

    async def get_cart_by_user_id(self, user_id: UUID) -> Cart | None:
        return next((c for c in self.carts.values() if c.user_id == user_id), None)

    async def get_cart_by_guest_token(self, guest_token: str) -> Cart | None:
        return next(
            (c for c in self.carts.values() if c.guest_token == guest_token), None
        )

    async def reassign_to_user(self, cart: Cart, *, user_id: UUID) -> Cart:
        cart.user_id = user_id
        cart.guest_token = None
        return cart

    async def delete_cart(self, cart: Cart) -> None:
        for item in list(cart.items):
            self.items.pop(item.id, None)
        self.carts.pop(cart.id, None)

    async def get_item_by_id(self, item_id: int) -> CartItem | None:
        return self.items.get(item_id)

    async def get_item_by_product(
        self, cart_id: int, product_id: int
    ) -> CartItem | None:
        return next(
            (
                i
                for i in self.items.values()
                if i.cart_id == cart_id and i.product_id == product_id
            ),
            None,
        )

    async def add_item(
        self, *, cart_id: int, product_id: int, quantity: int, price_cents: int
    ) -> CartItem:
        cart = next(c for c in self.carts.values() if c.id == cart_id)
        item = CartItem(
            id=self._next_item_id,
            cart_id=cart_id,
            product_id=product_id,
            quantity=quantity,
            price_cents_snapshot=price_cents,
        )

        item.cart = cart
        self.items[self._next_item_id] = item
        self._next_item_id += 1
        return item

    async def update_quantity(self, item: CartItem, quantity: int) -> CartItem:
        item.quantity = quantity
        return item

    async def remove_item(self, item: CartItem) -> None:
        self.items.pop(item.id, None)
        if item.cart is not None and item in item.cart.items:
            item.cart.items.remove(item)

    async def clear(self, cart: Cart) -> None:
        for item in list(cart.items):
            self.items.pop(item.id, None)
        cart.items.clear()


class FakeProductRepository:

    def __init__(self) -> None:
        self.products: dict[int, Product] = {}
        self._next_id = 1

    async def get_by_id(self, product_id: int) -> Product | None:
        return self.products.get(product_id)

    async def get_by_sku(self, sku: str) -> Product | None:
        return next((p for p in self.products.values() if p.sku == sku), None)

    async def get_by_slug(self, slug: str) -> Product | None:
        return next((p for p in self.products.values() if p.slug == slug), None)

    async def create(self, **fields) -> Product:
        fake_created_at = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(
            seconds=self._next_id
        )
        product = Product(
            id=self._next_id,
            created_at=fields.pop("created_at", fake_created_at),
            updated_at=fields.pop("updated_at", fake_created_at),
            **fields,
        )
        self.products[self._next_id] = product
        self._next_id += 1
        return product

    async def update(self, product: Product, **fields) -> Product:
        for key, value in fields.items():
            setattr(product, key, value)
        return product


@pytest.fixture
def fake_cart_repository() -> FakeCartRepository:
    return FakeCartRepository()


@pytest.fixture
def fake_product_repository() -> FakeProductRepository:
    return FakeProductRepository()
