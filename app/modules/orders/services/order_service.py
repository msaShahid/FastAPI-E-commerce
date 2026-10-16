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
from app.modules.orders.services.shipping import ShippingAddressInput, address_as_order_columns
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
        """
        Exactly one of address_id/shipping_address wins, in that order
        of precedence: an inline address is honored even for a logged-in
        user (shipping somewhere other than a saved address), otherwise
        a saved address is looked up and ownership-checked, otherwise
        (logged-in only) the user's default saved address is used. A
        guest has no saved addresses at all, so for a guest this only
        ever resolves shipping_address or raises.
        """
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
            default_address = await self.address_repository.get_default_for_user(user_id)
            if default_address is not None:
                return address_as_order_columns(default_address)

        # No address_repository wired in at all (e.g. a unit test
        # constructing OrderService with just the first three
        # positional args) and nothing supplied inline -- fall back to
        # no shipping snapshot rather than forcing every existing
        # caller to pass one. A real HTTP checkout always has an
        # address_repository (see order_deps.get_order_service) and the
        # API schema requires address_id or shipping_address, so this
        # branch is unreachable in production.
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
        """
        Idempotency is scoped per-identity (see the composite unique
        constraints on Order): a retry from this same user, or this
        same guest session, with this same key returns the existing
        order. Two different callers can use the identical key value
        without any collision or cross-identity leak -- the lookup
        itself is scoped to (user_id, key) or (guest_token, key), so
        there is no cross-identity case left to reject here.

        Exactly one of user_id/guest_token identifies who's checking
        out (mirrors CartOwner). A guest (user_id is None) must supply
        guest_email and a shipping_address -- there's no saved address
        or account to fall back to.
        """
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

        cart = await self.cart_repository.get_or_create_cart(user_id, guest_token=guest_token)
        if not cart.items:
            raise EmptyCartError()

        sorted_items = sorted(cart.items, key=lambda i: i.product_id)

        order_items_data = []
        subtotal_cents = 0

        # One query locks every product in the cart (in ascending id order, so
        # concurrent checkouts cannot deadlock) instead of one query per line.
        locked_products = await self.product_repository.get_many_for_update(
            [cart_item.product_id for cart_item in sorted_items]
        )

        remaining_stock = {pid: p.stock for pid, p in locked_products.items()}
        stock_deltas: dict[int, int] = {}

        for cart_item in sorted_items:
            product = locked_products.get(cart_item.product_id)

            if product is None or product.status != ProductStatus.ACTIVE:
                raise ProductUnavailableError()
            if remaining_stock[product.id] < cart_item.quantity:
                raise InsufficientStockError(available=remaining_stock[product.id])

            remaining_stock[product.id] -= cart_item.quantity
            stock_deltas[product.id] = stock_deltas.get(product.id, 0) - cart_item.quantity

            subtotal_cents += cart_item.price_cents_snapshot * cart_item.quantity
            order_items_data.append(
                {
                    "product_id": product.id,
                    "product_name_snapshot": product.name,
                    "price_cents_snapshot": cart_item.price_cents_snapshot,
                    "quantity": cart_item.quantity,
                }
            )

        # One UPDATE for every product's stock, not one per cart line.
        await self.product_repository.adjust_stock_many(locked_products, stock_deltas)

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
        order = await self.repository.get_by_guest_token(order_id, guest_token=guest_token)
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
        """
        Admin-only -- there is no is_admin parameter here because the
        access check happens at the router via the AdminUser dependency,
        the same way update_status relies on the router's AdminUser
        rather than re-checking a role here.
        """
        return await self.repository.list_all(offset=offset, limit=limit, status=status)

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

            locked_products = await self.product_repository.get_many_for_update(
                [item.product_id for item in order.items]
            )
            restock: dict[int, int] = {}
            for item in order.items:
                if item.product_id in locked_products:  # a deleted product has nothing to restock
                    restock[item.product_id] = restock.get(item.product_id, 0) + item.quantity
            await self.product_repository.adjust_stock_many(locked_products, restock)

            await self.repository.update_status(order, OrderStatus.CANCELLED, None)
            cancelled_order_ids.append(order.id)

        return cancelled_order_ids
