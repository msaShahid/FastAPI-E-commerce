from fastapi import APIRouter, status

from app.modules.auth.dependencies.auth import CurrentUser
from app.modules.cart.dependencies.cart_deps import CartServiceDep
from app.modules.cart.models.cart import Cart
from app.modules.cart.Schemas.cart import CartItemCreate, CartItemRead, CartItemUpdate, CartRead

cart_router = APIRouter(prefix="/cart", tags=["cart"])


def _to_cart_read(cart: Cart) -> CartRead:

    items = []
    subtotal = 0
    for item in cart.items:
        line_total = item.quantity * item.price_cents_snapshot
        subtotal += line_total
        items.append(
            CartItemRead(
                id=item.id,
                product=item.product,
                quantity=item.quantity,
                price_cents_snapshot=item.price_cents_snapshot,
                price_changed=item.price_cents_snapshot != item.product.price_cents,
                line_total_cents=line_total,
            )
        )
    return CartRead(items=items, subtotal_cents=subtotal, item_count=len(items))


@cart_router.get("", response_model=CartRead)
async def get_cart(current_user: CurrentUser, service: CartServiceDep) -> CartRead:
    cart = await service.get_cart(current_user.id)
    return _to_cart_read(cart)


@cart_router.post("/items", response_model=CartRead, status_code=status.HTTP_201_CREATED)
async def add_item(payload: CartItemCreate, current_user: CurrentUser, service: CartServiceDep) -> CartRead:
    cart = await service.add_item(
        user_id=current_user.id, product_id=payload.product_id, quantity=payload.quantity
    )
    return _to_cart_read(cart)


@cart_router.patch("/items/{item_id}", response_model=CartRead)
async def update_item(
    item_id: int, payload: CartItemUpdate, current_user: CurrentUser, service: CartServiceDep
) -> CartRead:
    cart = await service.update_item_quantity(
        user_id=current_user.id, item_id=item_id, quantity=payload.quantity
    )
    return _to_cart_read(cart)


@cart_router.delete("/items/{item_id}", response_model=CartRead)
async def remove_item(item_id: int, current_user: CurrentUser, service: CartServiceDep) -> CartRead:
    cart = await service.remove_item(user_id=current_user.id, item_id=item_id)
    return _to_cart_read(cart)


@cart_router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cart(current_user: CurrentUser, service: CartServiceDep) -> None:
    await service.clear_cart(current_user.id)