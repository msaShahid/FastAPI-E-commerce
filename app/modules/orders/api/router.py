from uuid import UUID

from fastapi import APIRouter, Depends, Header, status

from app.modules.auth.dependencies.auth import AdminUser, CurrentUser
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.orders.schemas.order import OrderRead, OrderStatusUpdate
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PageParams, PaginatedResponse

order_router = APIRouter(prefix="/orders", tags=["orders"])


@order_router.post(
    "/checkout", response_model=OrderRead, status_code=status.HTTP_201_CREATED
)
async def checkout(
    current_user: CurrentUser,
    service: OrderServiceDep,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
) -> OrderRead:

    order = await service.checkout(
        user_id=current_user.id, idempotency_key=idempotency_key
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
