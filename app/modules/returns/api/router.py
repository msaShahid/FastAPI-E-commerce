"""Customer returns: the customer's requests, and the administrator's handling of them."""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser, CurrentUser
from app.modules.payments.dependencies.lifecycle_deps import LifecycleServiceDep
from app.modules.returns.schemas.returns import (
    EligibilityRead,
    ReturnApprove,
    ReturnClose,
    ReturnCreate,
    ReturnRead,
    ReturnReceive,
    ReturnReject,
)
from app.modules.returns.services.return_service import ReturnService
from app.shared.pagination.schemas import PageParams, PaginatedResponse

returns_router = APIRouter(tags=["returns"])


def get_return_service(db: DbSession, audit: AuditServiceDep) -> ReturnService:
    return ReturnService(db, audit)


ServiceDep = Annotated[ReturnService, Depends(get_return_service)]


# ---------------------------------------------------------------------------------------------- the customer
@returns_router.get("/orders/{order_id}/returns/eligibility", response_model=EligibilityRead)
async def return_eligibility(order_id: uuid.UUID, user: CurrentUser, service: ServiceDep):
    """Can this order of yours be returned, until when, and how many of each item are still returnable?"""
    return await service.order_eligibility(user, order_id)


@returns_router.post("/orders/{order_id}/returns", response_model=ReturnRead, status_code=status.HTTP_201_CREATED)
async def request_return(order_id: uuid.UUID, payload: ReturnCreate, user: CurrentUser, service: ServiceDep):
    """Ask to send back some of the items of a shipped order, within the return period."""
    return await service.create(user, order_id, payload)


@returns_router.get("/orders/{order_id}/returns", response_model=PaginatedResponse[ReturnRead])
async def list_order_returns(order_id: uuid.UUID, user: CurrentUser, service: ServiceDep, params: PageParams = Depends()):
    total, items = await service.mine(user, params.offset, params.page_size, order_id=order_id)
    return PaginatedResponse[ReturnRead](items=items, total=total, page=params.page, page_size=params.page_size)


@returns_router.get("/returns", response_model=PaginatedResponse[ReturnRead])
async def list_my_returns(user: CurrentUser, service: ServiceDep, params: PageParams = Depends()):
    total, items = await service.mine(user, params.offset, params.page_size)
    return PaginatedResponse[ReturnRead](items=items, total=total, page=params.page, page_size=params.page_size)


@returns_router.get("/returns/{return_id}", response_model=ReturnRead)
async def get_my_return(return_id: uuid.UUID, user: CurrentUser, service: ServiceDep):
    return await service.get_mine(user, return_id)


@returns_router.post("/returns/{return_id}/cancel", response_model=ReturnRead)
async def cancel_my_return(return_id: uuid.UUID, user: CurrentUser, service: ServiceDep):
    """Withdraw a request that has not been received yet."""
    return await service.cancel(user, return_id)


# ---------------------------------------------------------------------------------------------- administrators
@returns_router.get("/admin/returns", response_model=PaginatedResponse[ReturnRead])
async def list_returns(
    _admin: AdminUser, service: ServiceDep, params: PageParams = Depends(),
    status_filter: Annotated[Literal["requested", "approved", "rejected", "received", "closed", "cancelled"] | None, Query(alias="status")] = None,
    order_id: uuid.UUID | None = None, from_date: date | None = None, to_date: date | None = None,
):
    total, items = await service.admin_list(status=status_filter, order_id=order_id, from_date=from_date, to_date=to_date, offset=params.offset, limit=params.page_size)
    return PaginatedResponse[ReturnRead](items=items, total=total, page=params.page, page_size=params.page_size)


@returns_router.get("/admin/returns/{return_id}", response_model=ReturnRead)
async def get_return(return_id: uuid.UUID, _admin: AdminUser, service: ServiceDep):
    return await service.admin_get(return_id)


@returns_router.post("/admin/returns/{return_id}/approve", response_model=ReturnRead)
async def approve_return(return_id: uuid.UUID, admin: AdminUser, service: ServiceDep, payload: ReturnApprove | None = None):
    return await service.approve(return_id, admin, payload.note if payload else None)


@returns_router.post("/admin/returns/{return_id}/reject", response_model=ReturnRead)
async def reject_return(return_id: uuid.UUID, payload: ReturnReject, admin: AdminUser, service: ServiceDep):
    """Refuse a request; the reason is shown to the customer."""
    return await service.reject(return_id, admin, payload.reason)


@returns_router.post("/admin/returns/{return_id}/receive", response_model=ReturnRead)
async def receive_return(return_id: uuid.UUID, payload: ReturnReceive, admin: AdminUser, service: ServiceDep):
    """The goods arrived. For EVERY line say how many arrived and whether they go back in stock (only for goods you can sell again)."""
    return await service.receive(return_id, admin, payload)


@returns_router.post("/admin/returns/{return_id}/refund", response_model=ReturnRead)
async def refund_return(return_id: uuid.UUID, admin: AdminUser, service: ServiceDep, lifecycle: LifecycleServiceDep):
    """Start the refund of the whole order. Only when received returns bring back EVERY unit of the order (NovaCart refunds whole orders):
    otherwise `409 return_not_full`, and the return is closed with a recorded resolution instead."""
    return await service.refund(return_id, admin, lifecycle)


@returns_router.post("/admin/returns/{return_id}/close", response_model=ReturnRead)
async def close_return(return_id: uuid.UUID, payload: ReturnClose, admin: AdminUser, service: ServiceDep):
    """Finish a return without NovaCart starting a refund: `refunded_outside` (you refunded the customer at your payment provider),
    `replacement_sent`, or `no_refund`. The note records what was done."""
    return await service.close(return_id, admin, payload)
