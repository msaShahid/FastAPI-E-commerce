"""Discount coupons: the customer's check of a code against their cart, and the administrator's management of coupons."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.database import DbSession
from app.core.rate_limit import limiter
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.cart.dependencies.cart_deps import CartServiceDep
from app.modules.cart.dependencies.cart_identity import CartOwnerDep
from app.modules.cart.services.cart_view import line_state
from app.modules.coupons.exceptions.coupon_exceptions import CouponNotApplicableError
from app.modules.coupons.models.coupon import Coupon
from app.modules.coupons.schemas.coupons import (
    CouponCheck,
    CouponCreate,
    CouponPreview,
    CouponRead,
    CouponUpdate,
    RedemptionRead,
)
from app.modules.coupons.services.coupon_service import (
    CouponAdminService,
    evaluate,
    find_by_code,
    status_of,
)
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.orders.exceptions.order_exceptions import CheckoutNotConfiguredError
from app.shared.pagination.schemas import PageParams, PaginatedResponse

coupons_router = APIRouter(tags=["coupons"])


def get_coupon_admin_service(db: DbSession, audit: AuditServiceDep) -> CouponAdminService:
    return CouponAdminService(db, audit)


AdminServiceDep = Annotated[CouponAdminService, Depends(get_coupon_admin_service)]


def _read(coupon: Coupon, used: int) -> CouponRead:
    return CouponRead(
        id=coupon.id, code=coupon.code, name=coupon.name, description=coupon.description, kind=coupon.kind, percent_off=coupon.percent_off,
        amount_off_cents=coupon.amount_off_cents, max_discount_cents=coupon.max_discount_cents, min_subtotal_cents=coupon.min_subtotal_cents,
        starts_at=coupon.starts_at, ends_at=coupon.ends_at, max_uses_total=coupon.max_uses_total,
        max_uses_per_customer=coupon.max_uses_per_customer, active=coupon.active, uses=used, state=status_of(coupon, used), created_at=coupon.created_at,
    )


# ---------------------------------------------------------------------------------------------- the customer
@coupons_router.post("/cart/coupon/preview", response_model=CouponPreview)
@limiter.limit("20/minute")
async def preview_coupon(request: Request, payload: CouponCheck, owner: CartOwnerDep, carts: CartServiceDep, orders: OrderServiceDep, db: DbSession):
    """What a coupon would do to the cart as it is now (nothing is reserved or used). Tax is worked out at checkout. To use the coupon, send
    the same code as `coupon_code` in the checkout request: it is checked again there. A code that cannot be used says why
    (`coupon_invalid`, `coupon_minimum_not_met`, `coupon_limit_reached`, `coupon_not_applicable`)."""
    cart = await carts.get_cart(owner.user_id, guest_token=owner.guest_token)
    if not cart.items:
        raise CouponNotApplicableError("Your cart is empty")
    subtotal = sum(line_state(item)[0] * item.quantity for item in cart.items)
    try:
        shipping: int | None = orders._calculate_shipping(subtotal)
    except CheckoutNotConfiguredError:
        shipping = None
    coupon = await find_by_code(db, payload.code)
    discount = await evaluate(db, coupon, subtotal_cents=subtotal, shipping_cents=shipping or 0, user_id=owner.user_id, guest_email=payload.guest_email)
    after = subtotal - discount.goods_cents
    if shipping is not None:
        shipping = 0 if discount.shipping_cents else orders._calculate_shipping(after)
    return CouponPreview(code=coupon.code, kind=coupon.kind, subtotal_cents=subtotal, discount_cents=discount.goods_cents,
                         subtotal_after_discount_cents=after, shipping_cents=shipping, shipping_discount_cents=discount.shipping_cents)


# ---------------------------------------------------------------------------------------------- administrators
@coupons_router.post("/admin/coupons", response_model=CouponRead, status_code=status.HTTP_201_CREATED)
async def create_coupon(payload: CouponCreate, admin: AdminUser, service: AdminServiceDep):
    return _read(await service.create(payload, admin), 0)


@coupons_router.get("/admin/coupons", response_model=PaginatedResponse[CouponRead])
async def list_coupons(
    _admin: AdminUser, service: AdminServiceDep, params: PageParams = Depends(), search: Annotated[str | None, Query(max_length=50)] = None,
    state: Literal["active", "disabled", "scheduled", "expired", "exhausted"] | None = None,
):
    total, rows = await service.list(search=search, state=state, offset=params.offset, limit=params.page_size)
    return PaginatedResponse[CouponRead](items=[_read(c, n) for c, n in rows], total=total, page=params.page, page_size=params.page_size)


@coupons_router.get("/admin/coupons/{coupon_id}", response_model=CouponRead)
async def get_coupon(coupon_id: int, _admin: AdminUser, service: AdminServiceDep):
    coupon, used = await service.get(coupon_id)
    return _read(coupon, used)


@coupons_router.patch("/admin/coupons/{coupon_id}", response_model=CouponRead)
async def update_coupon(coupon_id: int, payload: CouponUpdate, admin: AdminUser, service: AdminServiceDep):
    """Change a coupon. Its code and what it gives (kind, percentage, amount, cap) cannot change once an order has used it: switch it off
    (`active: false`) and create a new one instead."""
    coupon = await service.update(coupon_id, payload, admin)
    return _read(coupon, (await service.get(coupon_id))[1])


@coupons_router.delete("/admin/coupons/{coupon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_coupon(coupon_id: int, admin: AdminUser, service: AdminServiceDep) -> None:
    """Only a coupon no order has ever used."""
    await service.delete(coupon_id, admin)


@coupons_router.get("/admin/coupons/{coupon_id}/redemptions", response_model=PaginatedResponse[RedemptionRead])
async def list_redemptions(coupon_id: int, _admin: AdminUser, service: AdminServiceDep, params: PageParams = Depends()):
    """The orders that used this coupon, newest first (whatever their status)."""
    total, orders = await service.redemptions(coupon_id, offset=params.offset, limit=params.page_size)
    items = [RedemptionRead(order_id=o.id, status=o.status.value, discount_cents=o.discount_cents, total_cents=o.total_cents, user_id=o.user_id,
                            guest_email=o.guest_email, created_at=o.created_at) for o in orders]
    return PaginatedResponse[RedemptionRead](items=items, total=total, page=params.page, page_size=params.page_size)
