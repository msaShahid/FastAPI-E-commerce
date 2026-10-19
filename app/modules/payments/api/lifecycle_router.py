from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import DbSession
from app.core.jobs.models import BackgroundJob
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.orders.models.order import Order
from app.modules.payments.dependencies.lifecycle_deps import LifecycleServiceDep
from app.modules.payments.job_types import SWEEP_STALE_ORDERS
from app.modules.payments.schemas.payment import (
    LifecycleHealth,
    ReconciliationRead,
    ReconciliationResolve,
    RefundRead,
)
from app.shared.enums.order_status import OrderStatus
from app.shared.pagination.schemas import PageParams, PaginatedResponse

lifecycle_router = APIRouter(prefix="/admin", tags=["order-payment-lifecycle"])


@lifecycle_router.get("/refunds", response_model=PaginatedResponse[RefundRead])
async def list_refunds(
    _admin: AdminUser, lifecycle: LifecycleServiceDep, params: PageParams = Depends(), status_filter: str | None = None
):
    items, total = await lifecycle.refunds.list(status=status_filter, offset=params.offset, limit=params.page_size)
    return PaginatedResponse(items=[RefundRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)


@lifecycle_router.get("/reconciliations", response_model=PaginatedResponse[ReconciliationRead])
async def list_reconciliations(
    _admin: AdminUser, lifecycle: LifecycleServiceDep, params: PageParams = Depends(),
    status_filter: str | None = "open", kind: str | None = None,
):
    """Things about money that need a human: late or unmatched payments, mismatches, unconfirmed cancellations
    and refunds. Defaults to the open ones."""
    items, total = await lifecycle.reconciliations.list(
        status=status_filter, kind=kind, offset=params.offset, limit=params.page_size
    )
    return PaginatedResponse(items=[ReconciliationRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)


@lifecycle_router.post("/reconciliations/{record_id}/resolve", response_model=ReconciliationRead)
async def resolve_reconciliation(
    record_id: UUID, payload: ReconciliationResolve, admin: AdminUser, lifecycle: LifecycleServiceDep
):
    """Closes a record with a written note (e.g. "refunded in the Stripe dashboard"). Changes no order or money."""
    record = await lifecycle.resolve_reconciliation(record_id, admin.id, payload.resolution, payload.note)
    return ReconciliationRead.model_validate(record)


@lifecycle_router.post(
    "/reconciliations/{record_id}/refund", response_model=RefundRead, status_code=status.HTTP_202_ACCEPTED
)
async def refund_from_reconciliation(record_id: UUID, admin: AdminUser, lifecycle: LifecycleServiceDep):
    """The explicit, administrator-initiated way to refund a late payment. Nothing is ever refunded automatically."""
    _record, refund = await lifecycle.refund_from_reconciliation(record_id, admin.id)
    return RefundRead.model_validate(refund)


@lifecycle_router.get("/lifecycle/health", response_model=LifecycleHealth)
async def lifecycle_health(_admin: AdminUser, lifecycle: LifecycleServiceDep, db: DbSession) -> LifecycleHealth:
    """Monitoring for order expiry and refunds: is the worker sweeping, is anything stuck?"""
    settings = get_settings()
    interval = settings.order_expiry_sweep_interval_seconds
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=settings.pending_order_timeout_minutes)
    pending = Order.status == OrderStatus.PENDING
    stale = (await db.execute(select(func.count()).select_from(Order).where(pending, Order.created_at < cutoff))).scalar_one()
    oldest = (await db.execute(select(func.min(Order.created_at)).where(pending))).scalar_one()
    failed_jobs = (await db.execute(
        select(func.count()).select_from(BackgroundJob).where(BackgroundJob.status == "failed"))).scalar_one()
    last_sweep = (await db.execute(
        select(func.max(BackgroundJob.finished_at)).where(
            BackgroundJob.job_type == SWEEP_STALE_ORDERS, BackgroundJob.status == "succeeded"))).scalar_one()
    now = datetime.now(UTC)
    overdue = interval > 0 and (
        (last_sweep is None and stale > 0) or (last_sweep is not None and (now - last_sweep).total_seconds() > 3 * interval)
    )
    active = {}
    for state in ("requested", "pending", "uncertain"):
        _items, count = await lifecycle.refunds.list(status=state, offset=0, limit=1)
        active[state] = count
    return LifecycleHealth(
        stale_pending_orders=stale,
        oldest_pending_age_seconds=None if oldest is None else int((now.replace(tzinfo=None) - oldest).total_seconds()),
        open_reconciliations=await lifecycle.reconciliations.count_open(),
        active_refunds=active,
        failed_jobs=failed_jobs,
        sweep_interval_seconds=interval,
        last_sweep_succeeded_at=last_sweep,
        sweep_overdue=overdue,
    )
