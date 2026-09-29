from datetime import datetime
from uuid import UUID

from app.modules.addresses.exceptions.address_exceptions import AddressNotFoundError
from app.modules.addresses.repositories.address_repository import AddressRepository
from app.modules.cart.exceptions.cart_exceptions import (
    InsufficientStockError,
    ProductUnavailableError,
)
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.orders.exceptions.order_exceptions import (
    EmptyCartError,
    GuestEmailRequiredError,
    InvalidStatusTransitionError,
    OrderAccessForbiddenError,
    OrderNotFoundError,
    ShippingAddressRequiredError,
)
from app.modules.orders.models.order import Order
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.shipping import (
    ShippingAddressInput,
    address_as_order_columns,
)
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.order_status import ALLOWED_TRANSITIONS, OrderStatus
from app.shared.enums.product_status import ProductStatus


class OrderService:

    def __init__(
        self,
        repository: OrderRepository,
        cart_repository: CartRepository,
        product_repository: ProductRepository,
        address_repository: AddressRepository | None = None,
        *,
        flat_shipping_cents: int = 0,
        free_shipping_threshold_cents: int = 0,
        tax_rate_percent: float = 0.0,
    ) -> None:
        self.repository = repository
        self.cart_repository = cart_repository
        self.product_repository = product_repository
        self.address_repository = address_repository
        self.flat_shipping_cents = flat_shipping_cents
        self.free_shipping_threshold_cents = free_shipping_threshold_cents
        self.tax_rate_percent = tax_rate_percent

    def _calculate_shipping_and_tax(self, subtotal_cents: int) -> tuple[int, int]:
        # Flat rate, waived above the free-shipping threshold. A
        # threshold of 0 means "disabled" -- shipping is always the
        # flat rate in that case (there's no subtotal that waives it).
        threshold = self.free_shipping_threshold_cents
        if threshold > 0 and subtotal_cents >= threshold:
            shipping_cents = 0
        else:
            shipping_cents = self.flat_shipping_cents

        tax_cents = round(subtotal_cents * self.tax_rate_percent / 100)
        return shipping_cents, tax_cents

    async def _resolve_shipping_snapshot(
        self,
        *,
        user_id: UUID | None,
        is_guest: bool,
        address_id: int | None,
        shipping_address: ShippingAddressInput | None,
    ) -> dict:

        if shipping_address is not None:
            return shipping_address.as_order_columns()

        if is_guest:
            raise ShippingAddressRequiredError()

        if address_id is not None:
            if self.address_repository is None:
                raise ShippingAddressRequiredError()
            address = await self.address_repository.get_by_id(address_id)
            if address is None or address.user_id != user_id:
                raise AddressNotFoundError(address_id)
            return address_as_order_columns(address)

        if self.address_repository is not None:
            default_address = await self.address_repository.get_default_for_user(
                user_id
            )
            if default_address is not None:
                return address_as_order_columns(default_address)

        return {}

    async def checkout(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        guest_email: str | None = None,
        idempotency_key: str,
        address_id: int | None = None,
        shipping_address: ShippingAddressInput | None = None,
    ) -> Order:

        is_guest = user_id is None

        if is_guest and not guest_email:
            raise GuestEmailRequiredError()

        if is_guest:
            existing = await self.repository.get_by_idempotency_key(
                idempotency_key, guest_token=guest_token
            )
        else:
            existing = await self.repository.get_by_idempotency_key(
                idempotency_key, user_id=user_id
            )
        if existing is not None:
            return existing

        shipping_columns = await self._resolve_shipping_snapshot(
            user_id=user_id,
            is_guest=is_guest,
            address_id=address_id,
            shipping_address=shipping_address,
        )

        cart = await self.cart_repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )
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

        shipping_cents, tax_cents = self._calculate_shipping_and_tax(subtotal_cents)
        total_cents = subtotal_cents + shipping_cents + tax_cents

        order = await self.repository.create_order(
            user_id=user_id,
            guest_token=guest_token if is_guest else None,
            guest_email=guest_email if is_guest else None,
            idempotency_key=idempotency_key,
            items=order_items_data,
            subtotal_cents=subtotal_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
            **shipping_columns,
        )

        await self.cart_repository.clear(cart)
        return order

    async def get_guest_order(self, *, order_id: UUID, guest_token: str) -> Order:
        order = await self.repository.get_by_guest_token(
            order_id, guest_token=guest_token
        )
        if order is None:
            raise OrderNotFoundError(order_id)
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

    async def list_all_orders(
        self, *, offset: int, limit: int, status: OrderStatus | None = None
    ) -> tuple[list[Order], int]:

        return await self.repository.list_all(offset=offset, limit=limit, status=status)

    async def update_status(
        self,
        *,
        order_id: UUID,
        new_status: OrderStatus,
        changed_by_user_id: UUID | None,
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
                    await self.product_repository.increment_stock(
                        product, item.quantity
                    )

            await self.repository.update_status(order, OrderStatus.CANCELLED, None)
            cancelled_order_ids.append(order.id)

        return cancelled_order_ids
