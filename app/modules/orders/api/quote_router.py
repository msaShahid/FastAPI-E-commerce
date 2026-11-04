from fastapi import APIRouter, Request

from app.core.rate_limit import limiter
from app.modules.cart.dependencies.cart_deps import CartServiceDep
from app.modules.cart.dependencies.cart_identity import CartOwnerDep
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.orders.schemas.quote import CartQuote, QuoteRequest
from app.modules.orders.services.shipping import ShippingAddressInput

quote_router = APIRouter(prefix="/cart", tags=["cart"])


@quote_router.post("/quote", response_model=CartQuote)
@limiter.limit("30/minute")
async def quote_cart(request: Request, payload: QuoteRequest, owner: CartOwnerDep, carts: CartServiceDep, orders: OrderServiceDep) -> CartQuote:
    """What checkout would charge for this cart right now: subtotal, coupon discount, shipping, tax and total, worked out by the same rules as
    checkout (see `OrderService.quote`). Nothing is reserved or used. Works for guests and signed-in customers. Without an address a GST tax and the
    total are `null` with `requires_address` true; with one they are filled in. Amounts use CURRENT prices: if a line's price moved since it was
    added, `price_changed` is true and checkout will ask the customer to confirm the new price first."""
    cart = await carts.get_cart(owner.user_id, guest_token=owner.guest_token)
    address = ShippingAddressInput(**payload.shipping_address.model_dump()) if payload.shipping_address is not None else None
    return await orders.quote(
        cart=cart, user_id=owner.user_id, guest_email=payload.guest_email if owner.user_id is None else None,
        address_id=payload.address_id, shipping_address=address, coupon_code=payload.coupon_code,
    )
