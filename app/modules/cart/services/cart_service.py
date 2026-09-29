from uuid import UUID

from app.modules.cart.exceptions.cart_exceptions import (
    CartItemNotFoundError,
    InsufficientStockError,
    ProductUnavailableError,
)
from app.modules.cart.models.cart import Cart
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.product_status import ProductStatus


def _owns(cart: Cart, *, user_id: UUID | None, guest_token: str | None) -> bool:
    if user_id is not None:
        return cart.user_id == user_id
    return cart.guest_token == guest_token


class CartService:

    def __init__(
        self, repository: CartRepository, product_repository: ProductRepository
    ) -> None:
        self.repository = repository
        self.product_repository = product_repository

    async def _validate_product_available(
        self, product_id: int, requested_quantity: int
    ):
        product = await self.product_repository.get_by_id(product_id)
        if product is None or product.status != ProductStatus.ACTIVE:
            raise ProductUnavailableError()
        if product.stock < requested_quantity:
            raise InsufficientStockError(available=product.stock)
        return product

    async def get_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:
        return await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )

    async def add_item(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        product_id: int,
        quantity: int,
    ) -> Cart:
        cart = await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )
        product = await self._validate_product_available(product_id, quantity)

        existing = await self.repository.get_item_by_product(cart.id, product_id)
        if existing is not None:

            new_quantity = existing.quantity + quantity
            await self._validate_product_available(product_id, new_quantity)
            await self.repository.update_quantity(existing, new_quantity)
        else:
            await self.repository.add_item(
                cart_id=cart.id,
                product_id=product_id,
                quantity=quantity,
                price_cents=product.price_cents,
            )

        return await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )

    async def update_item_quantity(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        item_id: int,
        quantity: int,
    ) -> Cart:
        item = await self.repository.get_item_by_id(item_id)

        if item is None or not _owns(
            item.cart, user_id=user_id, guest_token=guest_token
        ):
            raise CartItemNotFoundError(item_id)

        await self._validate_product_available(item.product_id, quantity)
        await self.repository.update_quantity(item, quantity)

        return await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )

    async def remove_item(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        item_id: int,
    ) -> Cart:
        item = await self.repository.get_item_by_id(item_id)

        if item is None or not _owns(
            item.cart, user_id=user_id, guest_token=guest_token
        ):
            raise CartItemNotFoundError(item_id)

        await self.repository.remove_item(item)
        return await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )

    async def clear_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:
        cart = await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )
        await self.repository.clear(cart)
        return await self.repository.get_or_create_cart(
            user_id, guest_token=guest_token
        )

    async def merge_guest_cart_into_user(
        self, *, user_id: UUID, guest_token: str
    ) -> None:
        """
        Called right after a successful login/register when the caller
        had a guest cart cookie. Anything they added anonymously ends up
        in their real cart; nothing is lost and nothing is duplicated
        beyond summing quantities for a product already in both carts.
        """
        guest_cart = await self.repository.get_cart_by_guest_token(guest_token)
        if guest_cart is None:
            return

        existing_user_cart = await self.repository.get_cart_by_user_id(user_id)

        if existing_user_cart is None:
            # The user has no cart of their own yet -- just hand the
            # guest cart over instead of creating an empty one and
            # copying items into it one by one.
            await self.repository.reassign_to_user(guest_cart, user_id=user_id)
            return

        for guest_item in list(guest_cart.items):
            existing_item = await self.repository.get_item_by_product(
                existing_user_cart.id, guest_item.product_id
            )
            if existing_item is not None:
                await self.repository.update_quantity(
                    existing_item, existing_item.quantity + guest_item.quantity
                )
            else:
                await self.repository.add_item(
                    cart_id=existing_user_cart.id,
                    product_id=guest_item.product_id,
                    quantity=guest_item.quantity,
                    price_cents=guest_item.price_cents_snapshot,
                )

        await self.repository.delete_cart(guest_cart)
