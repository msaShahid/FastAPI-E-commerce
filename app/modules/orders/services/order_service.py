from datetime import datetime
from uuid import UUID

from app.modules.cart.exceptions.cart_exceptions import (
    InsufficientStockError,
    ProductUnavailableError,
)
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.orders.exceptions.order_exceptions import (
    EmptyCartError,
    IdempotencyKeyConflictError,
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
            if existing.user_id != user_id:
                raise IdempotencyKeyConflictError()
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

    async def get_by_id_unchecked(self, order_id: UUID) -> Order | None:
        """
        No ownership/authorization check. For internal, system-initiated
        callers only (e.g. webhook processing) that have no user context
        to check against -- never expose this path to an HTTP request.
        """
        return await self.repository.get_by_id(order_id)

    async def lock_order_for_update(self, order_id: UUID) -> Order:
        """
        Row-locks the order for the duration of the current transaction.
        Callers should already have verified access via get_order before
        calling this -- it does not re-check ownership.
        """
        order = await self.repository.get_by_id_for_update(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def list_my_orders(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[Order], int]:
        return await self.repository.list_for_user(user_id, offset=offset, limit=limit)

    async def update_status(
        self, *, order_id: UUID, new_status: OrderStatus, changed_by_user_id: UUID | None
    ) -> Order:
        order = await self.repository.get_by_id(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)

        if new_status not in ALLOWED_TRANSITIONS[order.status]:
            raise InvalidStatusTransitionError(order.status.value, new_status.value)

        return await self.repository.update_status(
            order, new_status, changed_by_user_id
        )

    async def cancel_stale_pending_orders(self, *, older_than: datetime) -> list[UUID]:
        """
        Checkout decrements stock immediately (see checkout() above), so
        a PENDING order that never gets paid holds that stock forever
        unless something releases it. This cancels every order that has
        been PENDING since before `older_than` and restocks its items.
        Intended to be called periodically by an outside scheduler (see
        scripts/cancel_stale_orders.py), never from a request.

        `older_than` must be naive (no tzinfo) -- orders.created_at is a
        naive DateTime column (by convention, UTC), unlike e.g.
        refresh_tokens.expires_at which is timezone-aware.

        Each order is looked up, locked, and re-checked individually --
        the initial listing is not itself locked, so by the time this
        gets to a given order, the customer may have just paid, or
        another reaper run may have already handled it. Re-checking
        status and age AFTER acquiring the row lock (the same guard
        create_payment_for_order relies on) is what makes that safe:
        the loser of that race just skips the order instead of
        double-cancelling or cancelling a now-paid order.
        """
        stale_order_ids = await self.repository.list_stale_pending_order_ids(
            older_than=older_than
        )

        cancelled_order_ids: list[UUID] = []
        for order_id in stale_order_ids:
            order = await self.repository.get_by_id_for_update(order_id)
            if order is None:
                continue
            if order.status != OrderStatus.PENDING or order.created_at >= older_than:
                continue  # paid, already cancelled, or no longer stale since we listed it

            for item in sorted(order.items, key=lambda i: i.product_id):
                product = await self.product_repository.get_by_id_for_update(
                    item.product_id
                )
                if product is not None:
                    await self.product_repository.increment_stock(product, item.quantity)

            await self.repository.update_status(order, OrderStatus.CANCELLED, None)
            cancelled_order_ids.append(order.id)

        return cancelled_order_ids
