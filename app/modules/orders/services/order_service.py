from uuid import UUID

from app.modules.cart.exceptions.cart_exceptions import (
    InsufficientStockError,
    ProductUnavailableError,
)
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.orders.exceptions.order_exceptions import (
    EmptyCartError,
    InvalidStatusTransitionError,
    OrderAccessForbiddenError,
    OrderNotFoundError,
)
from app.modules.orders.models.order import Order
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.order_status import ALLOWED_TRANSITIONS, OrderStatus
from app.shared.enums.product_status import ProductStatus


class OrderService:

    def __init__(
        self,
        repository: OrderRepository,
        cart_repository: CartRepository,
        product_repository: ProductRepository,
    ) -> None:
        self.repository = repository
        self.cart_repository = cart_repository
        self.product_repository = product_repository

    async def checkout(self, *, user_id: UUID, idempotency_key: str) -> Order:

        existing = await self.repository.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        cart = await self.cart_repository.get_or_create_cart(user_id)
        if not cart.items:
            raise EmptyCartError()

        sorted_items = sorted(cart.items, key=lambda i: i.product_id)

        order_items_data = []
        subtotal_cents = 0

        for cart_item in sorted_items:
            product = await self.product_repository.get_by_id_for_update(
                cart_item.product_id
            )

            if product is None or product.status != ProductStatus.ACTIVE:
                raise ProductUnavailableError()
            if product.stock < cart_item.quantity:
                raise InsufficientStockError(available=product.stock)

            await self.product_repository.decrement_stock(product, cart_item.quantity)

            subtotal_cents += cart_item.price_cents_snapshot * cart_item.quantity
            order_items_data.append(
                {
                    "product_id": product.id,
                    "product_name_snapshot": product.name,
                    "price_cents_snapshot": cart_item.price_cents_snapshot,
                    "quantity": cart_item.quantity,
                }
            )

        shipping_cents = 0
        tax_cents = 0
        total_cents = subtotal_cents + shipping_cents + tax_cents

        order = await self.repository.create_order(
            user_id=user_id,
            idempotency_key=idempotency_key,
            items=order_items_data,
            subtotal_cents=subtotal_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
        )

        await self.cart_repository.clear(cart)
        return order

    async def get_order(
        self, *, order_id: UUID, user_id: UUID, is_admin: bool
    ) -> Order:
        order = await self.repository.get_by_id(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if not is_admin and order.user_id != user_id:

            raise OrderAccessForbiddenError()
        return order

    async def list_my_orders(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[Order], int]:
        return await self.repository.list_for_user(user_id, offset=offset, limit=limit)

    async def update_status(
        self, *, order_id: UUID, new_status: OrderStatus, changed_by_user_id: UUID
    ) -> Order:
        order = await self.repository.get_by_id(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)

        if new_status not in ALLOWED_TRANSITIONS[order.status]:
            raise InvalidStatusTransitionError(order.status.value, new_status.value)

        return await self.repository.update_status(
            order, new_status, changed_by_user_id
        )
