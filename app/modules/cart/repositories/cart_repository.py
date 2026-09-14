from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem


class CartRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_or_create_cart(self, user_id: UUID) -> Cart:
        result = await self.db.execute(
            select(Cart)
            .where(Cart.user_id == user_id)
            .options(selectinload(Cart.items).selectinload(CartItem.product))
        )
        cart = result.scalar_one_or_none()
        if cart is not None:
            return cart

        cart = Cart(user_id=user_id)
        self.db.add(cart)
        await self.db.flush()

        cart.items = []
        return cart

    async def get_item_by_id(self, item_id: int) -> CartItem | None:
        result = await self.db.execute(
            select(CartItem)
            .where(CartItem.id == item_id)
            .options(selectinload(CartItem.product), selectinload(CartItem.cart))
        )
        return result.scalar_one_or_none()

    async def get_item_by_product(
        self, cart_id: int, product_id: int
    ) -> CartItem | None:
        result = await self.db.execute(
            select(CartItem).where(
                CartItem.cart_id == cart_id, CartItem.product_id == product_id
            )
        )
        return result.scalar_one_or_none()

    async def add_item(
        self, *, cart_id: int, product_id: int, quantity: int, price_cents: int
    ) -> CartItem:
        item = CartItem(
            cart_id=cart_id,
            product_id=product_id,
            quantity=quantity,
            price_cents_snapshot=price_cents,
        )
        self.db.add(item)
        await self.db.flush()
        await self.db.refresh(item, attribute_names=["product"])
        return item

    async def update_quantity(self, item: CartItem, quantity: int) -> CartItem:
        item.quantity = quantity
        await self.db.flush()
        return item

    async def remove_item(self, item: CartItem) -> None:
        await self.db.delete(item)
        await self.db.flush()

    async def clear(self, cart: Cart) -> None:
        for item in list(cart.items):
            await self.db.delete(item)
        await self.db.flush()
