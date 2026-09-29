from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem


class CartRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_or_create_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:

        if user_id is None and guest_token is None:
            raise ValueError("get_or_create_cart requires user_id or guest_token")

        condition = (
            Cart.user_id == user_id
            if user_id is not None
            else Cart.guest_token == guest_token
        )

        result = await self.db.execute(
            select(Cart)
            .where(condition)
            .options(selectinload(Cart.items).selectinload(CartItem.product))
        )
        cart = result.scalar_one_or_none()
        if cart is not None:
            return cart

        cart = Cart(user_id=user_id, guest_token=guest_token)
        self.db.add(cart)
        await self.db.flush()

        cart.items = []
        return cart

    async def get_cart_by_user_id(self, user_id: UUID) -> Cart | None:
        """
        Looked up WITHOUT creating one -- used by the guest-cart merge
        flow to tell "user has no cart yet" (reassign the guest cart
        wholesale) apart from "user already has a cart" (merge item by
        item instead) without the side effect of creating an empty cart
        just to answer that question.
        """
        result = await self.db.execute(
            select(Cart)
            .where(Cart.user_id == user_id)
            .options(selectinload(Cart.items).selectinload(CartItem.product))
        )
        return result.scalar_one_or_none()

    async def get_cart_by_guest_token(self, guest_token: str) -> Cart | None:
        """
        Looks up a guest cart WITHOUT creating one -- used only by the
        login/register merge flow, which should do nothing when the
        guest never actually added anything (no guest cart exists yet).
        """
        result = await self.db.execute(
            select(Cart)
            .where(Cart.guest_token == guest_token)
            .options(selectinload(Cart.items).selectinload(CartItem.product))
        )
        return result.scalar_one_or_none()

    async def reassign_to_user(self, cart: Cart, *, user_id: UUID) -> Cart:
        """
        Converts a guest cart into that user's cart in place, moving its
        items along with it. Only used when the user has no cart of
        their own yet -- see CartService.merge_guest_cart_into_user,
        which merges item-by-item instead when they already have one.
        """
        cart.user_id = user_id
        cart.guest_token = None
        await self.db.flush()
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

    async def delete_cart(self, cart: Cart) -> None:
        await self.db.delete(cart)
        await self.db.flush()
