from uuid import UUID

from app.modules.cart.exceptions.cart_exceptions import (
    CartFullError,
    CartItemNotFoundError,
    InsufficientStockError,
    ProductUnavailableError,
    VariantRequiredError,
    VariantUnavailableError,
)
from app.modules.cart.models.cart import Cart
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.products.services.product_view import effective_price
from app.shared.enums.product_status import ProductStatus
from app.shared.limits import MAX_CART_LINES


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

    async def _resolve_unit(self, product_id: int | None, variant_id: int | None, requested_quantity: int):
        """What is being bought, and what it costs NOW. Returns (product, variant or None, effective price).

        A simple product is bought by product id; a product with options only through one of its ACTIVE variants. Stock is checked
        against the unit actually sold (the variant, or the simple product). The price is always computed here, never taken from the client."""
        if variant_id is not None:
            variant = await self.product_repository.get_variant(variant_id)
            if variant is None or (product_id is not None and variant.product_id != product_id):
                raise VariantUnavailableError()
            product = await self.product_repository.get_by_id(variant.product_id)
            if product is None or product.status != ProductStatus.ACTIVE or variant.status != "active":
                raise VariantUnavailableError()
            if variant.stock < requested_quantity:
                raise InsufficientStockError(available=variant.stock)
            return product, variant, effective_price(variant, product.price_cents)
        product = await self.product_repository.get_by_id(product_id)
        if product is None or product.status != ProductStatus.ACTIVE:
            raise ProductUnavailableError()
        if (product.option_count or 0) > 0:  # (None on a product object that has not been saved yet)
            raise VariantRequiredError()
        if product.stock < requested_quantity:
            raise InsufficientStockError(available=product.stock)
        return product, None, product.price_cents

    async def attach_variant_details(self, cart: Cart) -> Cart:
        """Loads the variant (and its option labels) of every variant line, in at most two queries, so the response never lazy-loads.
        A cart of simple products costs nothing extra."""
        lines = [item for item in cart.items if item.variant_id is not None]
        for item in cart.items:
            item.variant_obj, item.variant_labels = None, []
        if not lines:
            return cart
        variants = {v.id: v for v in await self.product_repository.variants_by_ids([i.variant_id for i in lines])}
        labels: dict[int, tuple[str, str]] = {}
        for option in await self.product_repository.options_for(sorted({i.product_id for i in lines})):
            for value in option.values:
                labels[value.id] = (option.name, value.value)
        for item in lines:
            variant = variants.get(item.variant_id)
            item.variant_obj = variant
            if variant is not None:
                item.variant_labels = [labels[i] for i in (variant.value1_id, variant.value2_id, variant.value3_id)[: variant.option_count] if i in labels]
        return cart

    async def _reload(self, user_id, guest_token) -> Cart:
        return await self.attach_variant_details(await self.repository.get_or_create_cart(user_id, guest_token=guest_token))

    async def get_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:
        return await self._reload(user_id, guest_token)

    async def add_item(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        product_id: int | None = None,
        variant_id: int | None = None,
        quantity: int,
    ) -> Cart:
        cart = await self.repository.get_or_create_cart(user_id, guest_token=guest_token)
        product, variant, price = await self._resolve_unit(product_id, variant_id, quantity)

        # A simple product is looked up and added exactly as before; the variant argument appears only for a variant line.
        variant_args = (variant.id,) if variant else ()
        existing = await self.repository.get_item_by_product(cart.id, product.id, *variant_args)
        if existing is not None:

            new_quantity = existing.quantity + quantity
            await self._resolve_unit(product.id, variant.id if variant else None, new_quantity)
            await self.repository.update_quantity(existing, new_quantity)
        else:
            # A new line (more of a product already in the cart is fine). A
            # guest can fill a cart without logging in, so this is a real bound.
            if len(cart.items) >= MAX_CART_LINES:
                raise CartFullError(MAX_CART_LINES)
            await self.repository.add_item(
                cart_id=cart.id,
                product_id=product.id,
                quantity=quantity,
                price_cents=price,
                **({"variant_id": variant.id} if variant else {}),
            )

        return await self._reload(user_id, guest_token)

    async def update_item_quantity(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        item_id: int,
        quantity: int,
    ) -> Cart:
        item = await self.repository.get_item_by_id(item_id)

        if item is None or not _owns(item.cart, user_id=user_id, guest_token=guest_token):
            raise CartItemNotFoundError(item_id)

        await self._resolve_unit(item.product_id, item.variant_id, quantity)
        await self.repository.update_quantity(item, quantity)

        return await self._reload(user_id, guest_token)

    async def remove_item(
        self,
        *,
        user_id: UUID | None = None,
        guest_token: str | None = None,
        item_id: int,
    ) -> Cart:
        item = await self.repository.get_item_by_id(item_id)

        if item is None or not _owns(item.cart, user_id=user_id, guest_token=guest_token):
            raise CartItemNotFoundError(item_id)

        await self.repository.remove_item(item)
        return await self._reload(user_id, guest_token)

    async def clear_cart(
        self, user_id: UUID | None = None, *, guest_token: str | None = None
    ) -> Cart:
        cart = await self.repository.get_or_create_cart(user_id, guest_token=guest_token)
        await self.repository.clear(cart)
        return await self._reload(user_id, guest_token)

    async def merge_guest_cart_into_user(
        self, *, user_id: UUID, guest_token: str
    ) -> None:
        """
        Called right after a successful login/register when the caller
        had a guest cart cookie. Anything they added anonymously ends up
        in their real cart; nothing is lost and nothing is duplicated
        beyond summing quantities for a product already in both carts.

        Does nothing if there's no guest cart for this token (the common
        case -- most logins don't follow a guest shopping session).
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

        # One call for all the lines (no query per line); see the repository.
        await self.repository.merge_items_into_cart(
            existing_user_cart, list(guest_cart.items)
        )

        await self.repository.delete_cart(guest_cart)
