from fastapi import APIRouter, Request, status

from app.core.rate_limit import cart_limit, limiter
from app.modules.cart.dependencies.cart_deps import CartServiceDep
from app.modules.cart.dependencies.cart_identity import CartOwnerDep
from app.modules.cart.models.cart import Cart
from app.modules.cart.Schemas.cart import (
    CartItemCreate,
    CartItemUpdate,
    CartRead,
)
from app.modules.cart.services.cart_view import build_cart_read

cart_router = APIRouter(prefix="/cart", tags=["cart"])


def _to_cart_read(cart: Cart) -> CartRead:
    return build_cart_read(cart)


@cart_router.get("", response_model=CartRead)
async def get_cart(owner: CartOwnerDep, service: CartServiceDep) -> CartRead:
    cart = await service.get_cart(owner.user_id, guest_token=owner.guest_token)
    return _to_cart_read(cart)


@cart_router.post(
    "/items", response_model=CartRead, status_code=status.HTTP_201_CREATED
)
@limiter.limit(cart_limit)
async def add_item(
    request: Request, payload: CartItemCreate, owner: CartOwnerDep, service: CartServiceDep
) -> CartRead:
    cart = await service.add_item(
        user_id=owner.user_id,
        guest_token=owner.guest_token,
        product_id=payload.product_id,
        variant_id=payload.variant_id,
        quantity=payload.quantity,
    )
    return _to_cart_read(cart)


@cart_router.patch("/items/{item_id}", response_model=CartRead)
async def update_item(
    item_id: int, payload: CartItemUpdate, owner: CartOwnerDep, service: CartServiceDep
) -> CartRead:
    cart = await service.update_item_quantity(
        user_id=owner.user_id,
        guest_token=owner.guest_token,
        item_id=item_id,
        quantity=payload.quantity,
    )
    return _to_cart_read(cart)


@cart_router.delete("/items/{item_id}", response_model=CartRead)
async def remove_item(
    item_id: int, owner: CartOwnerDep, service: CartServiceDep
) -> CartRead:
    cart = await service.remove_item(
        user_id=owner.user_id, guest_token=owner.guest_token, item_id=item_id
    )
    return _to_cart_read(cart)


@cart_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cart(owner: CartOwnerDep, service: CartServiceDep) -> None:
    await service.clear_cart(owner.user_id, guest_token=owner.guest_token)
