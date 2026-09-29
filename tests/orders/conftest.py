from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.products.models.product import Product
from app.shared.enums.order_status import OrderStatus
from tests.addresses.conftest import FakeAddressRepository


class FakeOrderRepository:
    def __init__(self) -> None:
        self.orders: dict[UUID, Order] = {}
        self.status_changes: list[tuple] = []

    async def get_by_idempotency_key(
        self, key: str, *, user_id: UUID | None = None, guest_token: str | None = None
    ) -> Order | None:
        return next(
            (
                o
                for o in self.orders.values()
                if o.idempotency_key == key
                and (
                    (user_id is not None and o.user_id == user_id)
                    or (guest_token is not None and o.guest_token == guest_token)
                )
            ),
            None,
        )

    async def get_by_guest_token(self, order_id, *, guest_token: str) -> Order | None:
        order = self.orders.get(order_id)
        if order is not None and order.guest_token == guest_token:
            return order
        return None

    async def get_by_id(self, order_id: UUID) -> Order | None:
        return self.orders.get(order_id)

    async def get_by_id_for_update(self, order_id: UUID) -> Order | None:
        return self.orders.get(order_id)

    async def list_stale_pending_order_ids(self, *, older_than: datetime) -> list[UUID]:
        return [
            o.id
            for o in self.orders.values()
            if o.status == OrderStatus.PENDING
            and o.created_at is not None
            and o.created_at < older_than
        ]

    async def list_for_user(self, user_id: UUID, *, offset: int, limit: int):
        matching = [o for o in self.orders.values() if o.user_id == user_id]
        return matching[offset : offset + limit], len(matching)

    async def list_all(self, *, offset: int, limit: int, status: OrderStatus | None = None):
        matching = [
            o for o in self.orders.values() if status is None or o.status == status
        ]
        matching.sort(key=lambda o: o.created_at or datetime.min, reverse=True)
        return matching[offset : offset + limit], len(matching)

    async def create_order(
        self,
        *,
        user_id,
        idempotency_key,
        items,
        subtotal_cents,
        shipping_cents,
        tax_cents,
        total_cents,
        guest_token=None,
        guest_email=None,
        shipping_recipient_name=None,
        shipping_line1=None,
        shipping_line2=None,
        shipping_city=None,
        shipping_state=None,
        shipping_postal_code=None,
        shipping_country=None,
    ) -> Order:
        order_id = uuid4()
        order = Order(
            id=order_id,
            user_id=user_id,
            guest_token=guest_token,
            guest_email=guest_email,
            idempotency_key=idempotency_key,
            status=OrderStatus.PENDING,
            subtotal_cents=subtotal_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
            shipping_recipient_name=shipping_recipient_name,
            shipping_line1=shipping_line1,
            shipping_line2=shipping_line2,
            shipping_city=shipping_city,
            shipping_state=shipping_state,
            shipping_postal_code=shipping_postal_code,
            shipping_country=shipping_country,
        )
        # Build OrderItem objects WITHOUT setting order_id then also
        # appending -- assigning .order triggers back_populates to
        # append automatically (the exact bug from Stage 15's fake).
        order.items = []
        for i, data in enumerate(items, start=1):
            item = OrderItem(id=i, order_id=order_id, **data)
            order.items.append(item)
        self.orders[order_id] = order
        return order

    async def update_status(self, order: Order, new_status: OrderStatus, changed_by_user_id) -> Order:
        self.status_changes.append((order.status, new_status, changed_by_user_id))
        order.status = new_status
        return order


class FakeCartRepositoryForOrders:
    def __init__(self) -> None:
        self.carts: dict[UUID, Cart] = {}
        self._next_cart_id = 1
        self._next_item_id = 1

    async def get_or_create_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:
        key = user_id if user_id is not None else guest_token
        if key in self.carts:
            return self.carts[key]
        cart = Cart(id=self._next_cart_id, user_id=user_id, guest_token=guest_token)
        cart.items = []
        self.carts[key] = cart
        self._next_cart_id += 1
        return cart

    async def clear(self, cart: Cart) -> None:
        cart.items.clear()

    def add_item_directly(self, cart: Cart, product_id: int, quantity: int, price_cents: int):
        """Test helper -- seeds a cart without going through CartService."""
        item = CartItem(
            id=self._next_item_id,
            cart_id=cart.id,
            product_id=product_id,
            quantity=quantity,
            price_cents_snapshot=price_cents,
        )
        self._next_item_id += 1
        cart.items.append(item)
        return item


class FakeProductRepositoryForOrders:
    def __init__(self) -> None:
        self.products: dict[int, Product] = {}
        self._next_id = 1
        self.lock_calls: list[int] = []  # records the ORDER products were locked in

    async def get_by_id(self, product_id: int) -> Product | None:
        return self.products.get(product_id)

    async def get_by_id_for_update(self, product_id: int) -> Product | None:
        self.lock_calls.append(product_id)
        return self.products.get(product_id)

    async def decrement_stock(self, product: Product, quantity: int) -> Product:
        product.stock -= quantity
        return product

    async def increment_stock(self, product: Product, quantity: int) -> Product:
        product.stock += quantity
        return product

    async def create(self, **fields) -> Product:
        fake_created_at = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(seconds=self._next_id)
        product = Product(
            id=self._next_id,
            created_at=fields.pop("created_at", fake_created_at),
            updated_at=fields.pop("updated_at", fake_created_at),
            **fields,
        )
        self.products[self._next_id] = product
        self._next_id += 1
        return product


@pytest.fixture
def fake_order_repository() -> FakeOrderRepository:
    return FakeOrderRepository()


@pytest.fixture
def fake_cart_repository_for_orders() -> FakeCartRepositoryForOrders:
    return FakeCartRepositoryForOrders()


@pytest.fixture
def fake_product_repository_for_orders() -> FakeProductRepositoryForOrders:
    return FakeProductRepositoryForOrders()


@pytest.fixture
def fake_address_repository() -> FakeAddressRepository:
    return FakeAddressRepository()