from typing import Annotated
from uuid import UUID

from app.modules.orders.services.shipping import ShippingAddressInput
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, status

from app.modules.auth.dependencies.auth import AdminUser, CurrentUser, OptionalUser
from app.modules.cart.dependencies.cart_identity import GUEST_CART_COOKIE, CartOwnerDep
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.orders.exceptions.order_exceptions import GuestEmailRequiredError
from app.modules.orders.schemas.order import CheckoutRequest, OrderRead, OrderStatusUpdate
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PageParams, PaginatedResponse

order_router = APIRouter(prefix="/orders", tags=["orders"])


@order_router.post(
    "/checkout", response_model=OrderRead, status_code=status.HTTP_201_CREATED
)
async def checkout(
    current_user: OptionalUser,
    owner: CartOwnerDep,
    service: OrderServiceDep,
    payload: CheckoutRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> OrderRead:
    if current_user is None and not payload.guest_email:
        raise GuestEmailRequiredError()

    shipping_address = (
        ShippingAddressInput(**payload.shipping_address.model_dump())
        if payload.shipping_address is not None
        else None
    )

    order = await service.checkout(
        user_id=owner.user_id,
        guest_token=owner.guest_token,
        guest_email=payload.guest_email if current_user is None else None,
        idempotency_key=idempotency_key,
        address_id=payload.address_id,
        shipping_address=shipping_address,
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


@order_router.patch("/{order_id}/status", response_model=OrderRead)
async def update_order_status(
    order_id: UUID,
    payload: OrderStatusUpdate,
    admin: AdminUser,
    service: OrderServiceDep,
) -> OrderRead:

    order = await service.update_status(
        order_id=order_id,
        new_status=payload.status,
        changed_by_user_id=admin.id,
    )
    return OrderRead.model_validate(order)