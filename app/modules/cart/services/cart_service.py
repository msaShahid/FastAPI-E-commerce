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

    async def get_cart(self, user_id: UUID) -> Cart:
        return await self.repository.get_or_create_cart(user_id)

    async def add_item(self, *, user_id: UUID, product_id: int, quantity: int) -> Cart:
        cart = await self.repository.get_or_create_cart(user_id)
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

        return await self.repository.get_or_create_cart(user_id)

    async def update_item_quantity(
        self, *, user_id: UUID, item_id: int, quantity: int
    ) -> Cart:
        item = await self.repository.get_item_by_id(item_id)

        if item is None or item.cart.user_id != user_id:
            raise CartItemNotFoundError(item_id)

        await self._validate_product_available(item.product_id, quantity)
        await self.repository.update_quantity(item, quantity)

        return await self.repository.get_or_create_cart(user_id)

    async def remove_item(self, *, user_id: UUID, item_id: int) -> Cart:
        item = await self.repository.get_item_by_id(item_id)

        if item is None or item.cart.user_id != user_id:
            raise CartItemNotFoundError(item_id)

        await self.repository.remove_item(item)
        return await self.repository.get_or_create_cart(user_id)

    async def clear_cart(self, user_id: UUID) -> Cart:
        cart = await self.repository.get_or_create_cart(user_id)
        await self.repository.clear(cart)
        return await self.repository.get_or_create_cart(user_id)
