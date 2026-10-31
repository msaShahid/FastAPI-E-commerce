from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from app.core.config import HARDENED_ENVIRONMENTS, get_settings
from app.core.rate_limit import checkout_limit, limiter
from app.core.request_context import get_request_id
from app.modules.auth.dependencies.auth import AdminUser, CurrentUser, OptionalUser
from app.modules.cart.dependencies.cart_identity import (
    GUEST_CART_COOKIE,
    CartOwnerDep,
    guest_payment_cookie_name,
)
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.orders.exceptions.order_exceptions import (
    CancellationUnconfirmedError,
    GuestEmailRequiredError,
    PaymentInProgressError,
    PriceChangedError,
)
from app.modules.orders.schemas.order import (
    CheckoutRequest,
    OrderCancelRequest,
    OrderRead,
    OrderStatusUpdate,
)
from app.modules.orders.services.shipping import ShippingAddressInput
from app.modules.payments.dependencies.lifecycle_deps import LifecycleServiceDep
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PageParams, PaginatedResponse

order_router = APIRouter(prefix="/orders", tags=["orders"])


@order_router.post(
    "/checkout",
    response_model=OrderRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {
            "description": (
                "Either code=price_changed (cart prices changed; the cart is already "
                "updated, review the new total and check out again), or the "
                "Idempotency-Key was already used with a different request."
            )
        },
        503: {"description": "Shipping/tax are not configured; checkout is disabled."},
    },
)
@limiter.limit(checkout_limit)
async def checkout(
    request: Request,
    response: Response,
    current_user: OptionalUser,
    owner: CartOwnerDep,
    service: OrderServiceDep,
    payload: CheckoutRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=255),
) -> OrderRead | JSONResponse:
    if current_user is None and not payload.guest_email:
        raise GuestEmailRequiredError()

    shipping_address = (
        ShippingAddressInput(**payload.shipping_address.model_dump())
        if payload.shipping_address is not None
        else None
    )

    try:
        order = await service.checkout(
            user_id=owner.user_id,
            guest_token=owner.guest_token,
            guest_email=payload.guest_email if current_user is None else None,
            idempotency_key=idempotency_key,
            address_id=payload.address_id,
            shipping_address=shipping_address,
            coupon_code=payload.coupon_code,
        )
    except PriceChangedError as exc:
        # Returned (not raised) on purpose: an exception would roll back the cart
        # refresh, and the customer would be told "prices changed" forever.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": exc.message,
                "code": "price_changed",
                "changes": exc.changes,
                "new_subtotal_cents": exc.new_subtotal_cents,
                "request_id": get_request_id(request),
            },
        )
    if owner.is_guest:
        # The guest's payment capability: random, order-scoped, stored only as a hash, sent only to the payments endpoints and
        # never in a URL or a response body. It lives no longer than the unpaid-order window.
        token = await service.issue_guest_payment_token(order)
        if token is not None:
            settings = get_settings()
            response.set_cookie(
                key=guest_payment_cookie_name(order.id),
                value=token,
                httponly=True,
                samesite="lax",
                secure=settings.environment in HARDENED_ENVIRONMENTS,
                path=f"{settings.api_v1_prefix}/payments",
                max_age=settings.pending_order_timeout_minutes * 60,
            )
    return OrderRead.model_validate(order)


@order_router.get("", response_model=PaginatedResponse[OrderRead])
async def list_my_orders(
    current_user: CurrentUser,
    service: OrderServiceDep,
    params: PageParams = Depends(),
) -> PaginatedResponse[OrderRead]:
    orders, total = await service.list_my_orders(
        current_user.id, offset=params.offset, limit=params.page_size
    )
    return PaginatedResponse[OrderRead](
        items=[OrderRead.model_validate(o) for o in orders],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@order_router.get("/admin", response_model=PaginatedResponse[OrderRead])
async def list_all_orders(
    admin: AdminUser,
    service: OrderServiceDep,
    params: PageParams = Depends(),
    status_filter: OrderStatus | None = Query(default=None, alias="status"),
) -> PaginatedResponse[OrderRead]:

    orders, total = await service.list_all_orders(
        offset=params.offset, limit=params.page_size, status=status_filter
    )
    return PaginatedResponse[OrderRead](
        items=[OrderRead.model_validate(o) for o in orders],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@order_router.get("/guest/{order_id}", response_model=OrderRead)
async def get_guest_order(
    order_id: UUID,
    service: OrderServiceDep,
    guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
) -> OrderRead:

    if not guest_cart_token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    order = await service.get_guest_order(order_id=order_id, guest_token=guest_cart_token)
    return OrderRead.model_validate(order)


@order_router.get("/{order_id}", response_model=OrderRead)
async def get_order(
    order_id: UUID, current_user: CurrentUser, service: OrderServiceDep
) -> OrderRead:
    order = await service.get_order(
        order_id=order_id,
        user_id=current_user.id,
        is_admin=current_user.role == UserRole.ADMIN,
    )
    return OrderRead.model_validate(order)


@order_router.post("/{order_id}/cancel", response_model=OrderRead)
async def cancel_order(
    order_id: UUID,
    current_user: CurrentUser,
    service: OrderServiceDep,
    lifecycle: LifecycleServiceDep,
    payload: OrderCancelRequest | None = None,
) -> OrderRead:
    """
    Cancels an UNPAID (pending) order: the customer who owns it, or an administrator. The payment is made
    unpayable at the provider first, then the order is cancelled and its stock restocked together.

    A paid order cannot be cancelled here (409 cancellation_not_allowed): it is refunded by an administrator.
    If the provider cannot confirm the payment state the order is NOT cancelled (409 cancellation_unconfirmed)
    and the attempt is reconciled. Cancelling twice is safe.
    """
    is_admin = current_user.role == UserRole.ADMIN
    order = await service.get_order(order_id=order_id, user_id=current_user.id, is_admin=is_admin)  # access check
    reason = (payload.reason if payload and payload.reason else
              ("cancelled by an administrator" if is_admin else "cancelled by the customer"))
    result = await lifecycle.cancel_order(
        order.id, actor_user_id=current_user.id, source="admin" if is_admin else "customer", reason=reason
    )
    if result.outcome == "uncertain":
        raise CancellationUnconfirmedError()
    if result.outcome == "payment_in_progress":
        raise PaymentInProgressError()
    fresh = await service.get_order(order_id=order.id, user_id=current_user.id, is_admin=is_admin)
    return OrderRead.model_validate(fresh)


@order_router.patch("/{order_id}/status", response_model=OrderRead)
async def update_order_status(
    order_id: UUID,
    payload: OrderStatusUpdate,
    admin: AdminUser,
    lifecycle: LifecycleServiceDep,
    service: OrderServiceDep,
) -> OrderRead:
    """
    Administrator status change. Only `fulfilled` (from `paid`) is allowed. `paid`, `refunded` and `cancelled`
    are refused (409 status_change_not_allowed) because setting them by hand would fabricate a payment, claim
    an unconfirmed refund, or cancel without payment/stock handling: those go through the payment provider,
    POST /payments/{order_id}/refund, and POST /orders/{order_id}/cancel.
    """
    await lifecycle.admin_change_status(order_id, payload.status, admin.id)
    order = await service.get_order(order_id=order_id, user_id=admin.id, is_admin=True)
    return OrderRead.model_validate(order)
